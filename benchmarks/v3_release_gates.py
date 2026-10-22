"""Validate and sign exact-candidate V3 release-gate evidence bundles."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

GATE_IDS = tuple(f"R{number}" for number in range(250, 267))
REQUIRED_PLATFORMS = (
    "linux-py312", "linux-py313", "macos-py312", "macos-py313", "windows-py312", "windows-py313",
)
SIGNER_FINGERPRINT = "D3897E8FF704149D609ACF878C118C04263D2A4C"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("evidence artifact path must be a nonempty relative path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError("evidence artifact escapes evidence directory") from None
    if not path.is_file():
        raise ValueError("evidence artifact is not a regular file")
    return path


def _check_record(record: object, gate_id: str, candidate_sha: str, evidence_dir: Path) -> list[dict]:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError(f"{gate_id}: unsupported evidence record schema")
    if record.get("gate_id") != gate_id:
        raise ValueError(f"{gate_id}: record gate ID does not match its filename")
    if record.get("candidate_sha") != candidate_sha:
        raise ValueError(f"{gate_id}: candidate SHA does not match the current candidate")
    for name in ("command_set_sha256", "fixture_set_sha256"):
        if not isinstance(record.get(name), str) or not _SHA256.fullmatch(record[name]):
            raise ValueError(f"{gate_id}: {name} must be a lowercase SHA-256 digest")
    environment = record.get("environment")
    if not isinstance(environment, dict) or any(not isinstance(environment.get(key), str) or not environment[key]
                                                for key in ("runner", "os", "python")):
        raise ValueError(f"{gate_id}: runner, OS, and Python environment details are required")
    if not isinstance(record.get("dependencies"), dict):
        raise ValueError(f"{gate_id}: dependency inventory must be an object")
    for path_key, hash_key, description in (
        ("command_set_path", "command_set_sha256", "command-set"),
        ("fixture_set_path", "fixture_set_sha256", "fixture-set"),
    ):
        path = _contained_file(evidence_dir, record.get(path_key))
        if _sha256(path) != record[hash_key]:
            raise ValueError(f"{gate_id}: {description} hash mismatch")
    result = record.get("result")
    if not isinstance(result, str) or result not in {"passed", "failed", "inconclusive"}:
        raise ValueError(f"{gate_id}: invalid result")
    if result != "passed":
        if not isinstance(record.get("failure_class"), str) or not record["failure_class"].strip():
            raise ValueError(f"{gate_id}: nonpassing evidence requires a failure class")
        raise ValueError(f"{gate_id}: gate did not pass ({record['failure_class']})")
    if record.get("failure_class") is not None:
        raise ValueError(f"{gate_id}: passing evidence cannot have a failure class")
    refs = record.get("evidence")
    if not isinstance(refs, list) or not refs:
        raise ValueError(f"{gate_id}: at least one evidence artifact is required")
    artifacts = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str) or not _SHA256.fullmatch(ref["sha256"]):
            raise ValueError(f"{gate_id}: invalid evidence artifact reference")
        path = _contained_file(evidence_dir, ref.get("path"))
        actual = _sha256(path)
        if actual != ref["sha256"]:
            raise ValueError(f"{gate_id}: evidence hash mismatch for {ref['path']}")
        artifacts.append({"path": path.relative_to(evidence_dir).as_posix(), "sha256": actual})
    if gate_id == "R266":
        platforms = record.get("platform_results")
        if not isinstance(platforms, list) or len(platforms) != len(REQUIRED_PLATFORMS):
            raise ValueError("R266: platform matrix must contain exactly six required platforms")
        if any(not isinstance(item, dict) or not isinstance(item.get("platform"), str)
               for item in platforms):
            raise ValueError("R266: platform matrix must contain exactly six required platforms")
        indexed = {item["platform"]: item for item in platforms}
        if set(indexed) != set(REQUIRED_PLATFORMS):
            raise ValueError("R266: platform matrix must contain exactly six required platforms")
        refs = record.get("evidence")
        artifact_paths = {ref["path"] for ref in refs
                          if isinstance(ref, dict) and isinstance(ref.get("path"), str)} if isinstance(refs, list) else set()
        for platform in REQUIRED_PLATFORMS:
            item = indexed[platform]
            if any(item.get(check) != "passed" for check in ("unit", "integration", "acceptance")):
                raise ValueError(f"R266: platform validation failed for {platform}")
            if not isinstance(item.get("soak_seconds"), int) or item["soak_seconds"] < 1800:
                raise ValueError(f"R266: platform {platform} has not passed the 30-minute soak")
            evidence_paths = item.get("evidence_paths")
            if not isinstance(evidence_paths, list) or not evidence_paths or any(
                    not isinstance(path, str) or path not in artifact_paths for path in evidence_paths):
                raise ValueError(f"R266: platform evidence is missing or unlinked for {platform}")
    return artifacts


def validate_evidence_bundle(evidence_dir: Path, candidate_sha: str, *, verify_signature) -> dict:
    """Validate every signed gate record and return a manifest bound to candidate_sha."""
    evidence_dir = Path(evidence_dir).resolve(strict=True)
    if not _GIT_SHA.fullmatch(candidate_sha):
        raise ValueError("candidate SHA must be a full Git object ID")
    gates = {}
    for gate_id in GATE_IDS:
        try:
            record_path = _contained_file(evidence_dir, f"{gate_id}.json")
            signature_path = _contained_file(evidence_dir, f"{gate_id}.json.asc")
        except ValueError:
            raise ValueError(f"missing gate record or signature for {gate_id}")
        if not verify_signature(record_path, signature_path):
            raise ValueError(f"{gate_id}: signature verification failed")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{gate_id}: invalid JSON record: {exc}") from None
        artifacts = _check_record(record, gate_id, candidate_sha, evidence_dir)
        gates[gate_id] = {
            "record_path": record_path.name,
            "record_sha256": _sha256(record_path),
            "signature_path": signature_path.name,
            "signature_sha256": _sha256(signature_path),
            "command_set_sha256": record["command_set_sha256"],
            "fixture_set_sha256": record["fixture_set_sha256"],
            "command_set_path": record["command_set_path"],
            "fixture_set_path": record["fixture_set_path"],
            "environment": record["environment"],
            "dependencies": record["dependencies"],
            "evidence": artifacts,
            **({"platform_results": record["platform_results"]} if gate_id == "R266" else {}),
        }
    return {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "signer_fingerprint": SIGNER_FINGERPRINT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": "passed",
        "gates": gates,
    }


def _gpg_verify(record_path: Path, signature_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["gpg", "--batch", "--status-fd", "1", "--verify", str(signature_path), str(record_path)],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            signer = fields[2] if len(fields) > 2 else ""
            primary = fields[11] if len(fields) > 11 else signer
            return SIGNER_FINGERPRINT in {signer, primary}
    return False


def _current_candidate(repo: Path) -> str:
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                         check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           check=True, capture_output=True, text=True, timeout=15).stdout
    if dirty:
        raise ValueError("candidate worktree must be clean before release evidence is validated")
    return head


def _require_outside_checkout(path: Path, repo: Path, label: str) -> None:
    try:
        path.relative_to(repo)
    except ValueError:
        return
    raise ValueError(f"{label} must be outside the candidate checkout")


def _write_signed_manifest(manifest: dict, destination: Path) -> None:
    destination = destination.resolve()
    signature_destination = destination.with_name(destination.name + ".asc")
    if destination.exists() or signature_destination.exists():
        raise ValueError("manifest output already exists; choose a new path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="v3-release-evidence-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        record = temporary / destination.name
        signature = temporary / f"{destination.name}.asc"
        record.write_bytes(payload)
        try:
            result = subprocess.run(
                ["gpg", "--batch", "--yes", "--local-user", SIGNER_FINGERPRINT,
                 "--armor", "--detach-sign", "--output", str(signature), str(record)],
                check=False, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"could not sign release manifest: {exc}") from None
        if result.returncode or not signature.is_file() or not _gpg_verify(record, signature):
            raise ValueError("release manifest GPG signing or fingerprint verification failed")
        os.replace(signature, signature_destination)
        try:
            os.replace(record, destination)
        except OSError:
            try:
                signature_destination.unlink()
            except OSError:
                pass
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve(strict=True)
        evidence_dir = args.evidence_dir.resolve(strict=True)
        manifest_path = args.manifest.resolve()
        _require_outside_checkout(evidence_dir, repo, "evidence directory")
        _require_outside_checkout(manifest_path, repo, "manifest")
        candidate = _current_candidate(repo)
        manifest = validate_evidence_bundle(evidence_dir, candidate, verify_signature=_gpg_verify)
        _write_signed_manifest(manifest, manifest_path)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"release blocked: {exc}", file=sys.stderr)
        return 1
    print(f"release evidence passed for {candidate}; signed manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
