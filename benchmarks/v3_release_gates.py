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
LIMITED_RELEASE_SCOPE = "openkyrozen-v3-preview-linux-macos-sandbox-required-v1"
WAIVABLE_INCONCLUSIVE_GATES = {"R259", "R262", "R263", "R264"}
WAIVABLE_FAILURE_CLASSES = {"provider_upstream_error"}
SAFETY_UNIT_LOGS = tuple(
    f"platforms/{platform}/unit.log"
    for platform in ("linux-py312", "linux-py313", "macos-py312", "macos-py313")
)
SAFETY_TESTS = (
    "test_apply_patch_is_atomic_stale_safe_and_rejects_ambiguous_hunks",
    "test_edit_file_requires_matching_hash_and_unique_old_text",
    "test_required_unavailable_sandbox_blocks_command_before_execution",
    "test_installed_but_unusable_backend_blocks_when_sandbox_is_required",
    "test_linux_sandbox_allows_workspace_writes_and_denies_sibling_writes",
    "test_macos_sandbox_allows_workspace_writes_and_denies_sibling_writes",
    "test_define_tool_text_is_rejected_without_registration",
    "test_full_capability_profile_does_not_grant_dynamic_registration",
    "test_approval_decisions_are_scoped_versioned_history_not_authority",
    "test_failed_migration_rolls_back_schema_and_version",
    "test_incompatible_existing_table_rolls_back_migration",
)


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
        if result == "failed":
            raise ValueError(f"{gate_id}: failed evidence cannot be waived ({record['failure_class']})")
    elif record.get("failure_class") is not None:
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
            if platform.startswith("windows-"):
                checks_failed = (
                    item.get("unit") != "not_run"
                    or item.get("integration") != "not_run"
                    or item.get("supported_features") != "passed"
                    or item.get("acceptance") != "passed"
                    or item.get("tui_interactive_acceptance") != "not_run_posix_pty_unavailable"
                )
            else:
                checks_failed = any(item.get(check) != "passed"
                                    for check in ("unit", "integration", "acceptance"))
            if checks_failed:
                raise ValueError(f"R266: platform validation failed for {platform}")
            if not isinstance(item.get("soak_seconds"), int) or item["soak_seconds"] < 1800:
                raise ValueError(f"R266: platform {platform} has not passed the 30-minute soak")
            evidence_paths = item.get("evidence_paths")
            if not isinstance(evidence_paths, list) or not evidence_paths or any(
                    not isinstance(path, str) or path not in artifact_paths for path in evidence_paths):
                raise ValueError(f"R266: platform evidence is missing or unlinked for {platform}")
    return artifacts


def _verify_safety_logs(gate_id: str, evidence_dir: Path) -> None:
    for relative in SAFETY_UNIT_LOGS:
        log = _contained_file(evidence_dir, relative).read_text(encoding="utf-8", errors="replace")
        if not re.search(r"(?m)^Ran \d+ tests? in [^\n]+$", log) or not re.search(r"(?m)^OK(?: \(skipped=\d+\))?$", log):
            raise ValueError(f"{gate_id}: safety test evidence is missing or did not pass for {relative}")
        methods = SAFETY_TESTS
        if relative.startswith("platforms/linux-"):
            methods = tuple(method for method in methods if not method.startswith("test_macos_sandbox"))
        else:
            methods = tuple(method for method in methods if not method.startswith("test_linux_sandbox"))
        for method in methods:
            if not any(line.startswith(method + " (") and line.endswith(" ... ok") for line in log.splitlines()):
                raise ValueError(f"{gate_id}: safety test evidence is missing or did not pass: {method}")


def _check_waiver(gate_id: str, record_path: Path, candidate_sha: str, release_scope: str,
                  evidence_dir: Path, records: dict, *, verify_signature) -> dict:
    if gate_id not in WAIVABLE_INCONCLUSIVE_GATES:
        raise ValueError(f"{gate_id}: inconclusive gate is not waivable")
    record = records[gate_id]["record"]
    if record.get("failure_class") not in WAIVABLE_FAILURE_CLASSES:
        raise ValueError(f"{gate_id}: failure class is non-waivable")
    waiver_path = _contained_file(evidence_dir, f"waivers/{gate_id}.json")
    signature_path = _contained_file(evidence_dir, f"waivers/{gate_id}.json.asc")
    if not verify_signature(waiver_path, signature_path):
        raise ValueError(f"{gate_id}: waiver signature verification failed")
    try:
        waiver = json.loads(waiver_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{gate_id}: invalid waiver JSON: {exc}") from None
    if not isinstance(waiver, dict) or waiver.get("schema_version") != 1:
        raise ValueError(f"{gate_id}: unsupported waiver schema")
    if waiver.get("gate_id") != gate_id:
        raise ValueError(f"{gate_id}: waiver gate ID does not match its filename")
    if waiver.get("candidate_sha") != candidate_sha:
        raise ValueError(f"{gate_id}: waiver candidate SHA does not match the current candidate")
    if waiver.get("release_scope") != release_scope:
        raise ValueError(f"{gate_id}: waiver release scope does not match the manifest scope")
    if waiver.get("approver_identity") != SIGNER_FINGERPRINT:
        raise ValueError(f"{gate_id}: waiver approver does not match the pinned release owner")
    if not isinstance(waiver.get("rationale"), str) or not waiver["rationale"].strip():
        raise ValueError(f"{gate_id}: waiver rationale is required")
    limitations = waiver.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
            not isinstance(item, str) or not item.strip() for item in limitations):
        raise ValueError(f"{gate_id}: waiver limitations are required")
    has_expiry = isinstance(waiver.get("expires_at"), str) and bool(waiver["expires_at"].strip())
    has_review = isinstance(waiver.get("review_condition"), str) and bool(waiver["review_condition"].strip())
    if has_expiry == has_review:
        raise ValueError(f"{gate_id}: specify exactly one waiver expiry or review condition")
    if has_expiry:
        try:
            expiry = datetime.fromisoformat(waiver["expires_at"].replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{gate_id}: waiver expiry must be an ISO-8601 timestamp") from None
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError(f"{gate_id}: waiver expiry must be a future timezone-aware timestamp")
    refs = waiver.get("evidence")
    if not isinstance(refs, list) or not refs:
        raise ValueError(f"{gate_id}: waiver evidence references are required")
    artifacts = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str) or not _SHA256.fullmatch(ref["sha256"]):
            raise ValueError(f"{gate_id}: invalid waiver evidence reference")
        path = _contained_file(evidence_dir, ref.get("path"))
        actual = _sha256(path)
        if actual != ref["sha256"]:
            raise ValueError(f"{gate_id}: waiver evidence hash mismatch for {ref['path']}")
        artifacts.append({"path": path.relative_to(evidence_dir).as_posix(), "sha256": actual})
    by_path = {ref["path"]: ref["sha256"] for ref in artifacts}
    required = {f"{gate_id}.json": _sha256(record_path), "R266.json": records["R266"]["record_sha256"]}
    required.update({path: _sha256(_contained_file(evidence_dir, path)) for path in SAFETY_UNIT_LOGS})
    if any(by_path.get(path) != digest for path, digest in required.items()):
        raise ValueError(f"{gate_id}: missing safety test evidence or evidence for the inconclusive record")
    r266_refs = {ref["path"]: ref["sha256"] for ref in records["R266"]["artifacts"]}
    if any(r266_refs.get(path) != digest for path, digest in required.items() if path in SAFETY_UNIT_LOGS):
        raise ValueError(f"{gate_id}: safety test evidence is not bound by the signed R266 platform record")
    _verify_safety_logs(gate_id, evidence_dir)
    return {
        "record_path": waiver_path.relative_to(evidence_dir).as_posix(),
        "record_sha256": _sha256(waiver_path),
        "signature_path": signature_path.relative_to(evidence_dir).as_posix(),
        "signature_sha256": _sha256(signature_path),
        "approver_identity": waiver["approver_identity"],
        "release_scope": waiver["release_scope"],
        "rationale": waiver["rationale"],
        "limitations": limitations,
        **({"expires_at": waiver["expires_at"]} if has_expiry else {"review_condition": waiver["review_condition"]}),
        "evidence": artifacts,
    }


def validate_evidence_bundle(evidence_dir: Path, candidate_sha: str, *, verify_signature,
                             limited_evidence: bool = False, release_scope: str | None = None) -> dict:
    """Validate full evidence, or a signed, exact-candidate limited-evidence preview."""
    evidence_dir = Path(evidence_dir).resolve(strict=True)
    if not _GIT_SHA.fullmatch(candidate_sha):
        raise ValueError("candidate SHA must be a full Git object ID")
    if limited_evidence:
        if release_scope != LIMITED_RELEASE_SCOPE:
            raise ValueError(f"limited-evidence preview scope must be {LIMITED_RELEASE_SCOPE}")
        waivers_dir = (evidence_dir / "waivers").resolve(strict=True)
    elif release_scope is not None:
        raise ValueError("release scope is only valid for limited-evidence preview")
    records = {}
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
        records[gate_id] = {
            "record": record,
            "artifacts": artifacts,
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
        }
    inconclusive = [gate_id for gate_id in GATE_IDS if records[gate_id]["record"]["result"] == "inconclusive"]
    if not limited_evidence and inconclusive:
        gate_id = inconclusive[0]
        raise ValueError(f"{gate_id}: gate did not pass ({records[gate_id]['record']['failure_class']})")
    if limited_evidence and not inconclusive:
        raise ValueError("limited-evidence preview requires at least one inconclusive gate waiver")
    gates = {}
    for gate_id in GATE_IDS:
        item = records[gate_id]
        record = item["record"]
        result = record["result"]
        entry = {key: value for key, value in item.items() if key not in {"record", "artifacts"}}
        entry["evidence_result"] = result
        entry["status"] = "passed" if result == "passed" else "inconclusive"
        if result == "inconclusive":
            entry["waiver"] = _check_waiver(
                gate_id, evidence_dir / f"{gate_id}.json", candidate_sha, release_scope,
                evidence_dir, records, verify_signature=verify_signature,
            )
            entry["status"] = "waived"
        if gate_id == "R266":
            entry["platform_results"] = record["platform_results"]
        gates[gate_id] = entry
    waiver_files = {path.name for path in waivers_dir.glob("R*.json")} if limited_evidence else set()
    expected_files = {f"{gate_id}.json" for gate_id in inconclusive} if limited_evidence else set()
    if waiver_files != expected_files:
        raise ValueError("waiver files must match exactly the inconclusive gates")
    waived_gates = [gate_id for gate_id in GATE_IDS if gates[gate_id]["status"] == "waived"]
    limitations = sorted({limit for gate_id in waived_gates for limit in gates[gate_id]["waiver"]["limitations"]})
    return {
        "schema_version": 2,
        "candidate_sha": candidate_sha,
        "signer_fingerprint": SIGNER_FINGERPRINT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": "limited-evidence" if limited_evidence else "passed",
        "release_classification": "limited-evidence-preview" if limited_evidence else "full",
        "release_scope": release_scope,
        "release_limitations": limitations,
        "waived_gates": waived_gates,
        "gates": gates,
    }


def _gpg_verify(record_path: Path, signature_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["gpg", "--batch", "--no-auto-check-trustdb", "--trust-model", "always",
             "--status-fd", "1", "--verify", str(signature_path), str(record_path)],
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
        os.link(signature, signature_destination)
        try:
            os.link(record, destination)
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
    parser.add_argument("--limited-evidence-preview", action="store_true",
                        help="prepare a clearly labeled preview with signed waivers for eligible inconclusive gates")
    parser.add_argument("--release-scope", help="scope ID bound by every signed preview waiver")
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve(strict=True)
        evidence_dir = args.evidence_dir.resolve(strict=True)
        manifest_path = args.manifest.resolve()
        _require_outside_checkout(evidence_dir, repo, "evidence directory")
        _require_outside_checkout(manifest_path, repo, "manifest")
        candidate = _current_candidate(repo)
        manifest = validate_evidence_bundle(
            evidence_dir, candidate, verify_signature=_gpg_verify,
            limited_evidence=args.limited_evidence_preview, release_scope=args.release_scope,
        )
        _write_signed_manifest(manifest, manifest_path)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"release blocked: {exc}", file=sys.stderr)
        return 1
    status = "limited-evidence preview prepared" if args.limited_evidence_preview else "release evidence passed"
    print(f"{status} for {candidate}; signed manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
