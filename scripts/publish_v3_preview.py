#!/usr/bin/env python3
"""Build and publish an exact-candidate V3 preview after signed evidence passes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import yaml

from benchmarks.v3_release_gates import (
    LIMITED_RELEASE_SCOPE, SIGNER_FINGERPRINT, _current_candidate, _gpg_verify, _read_signed_pair,
    _require_outside_checkout, _write_signed_manifest, verify_signed_preview_manifest,
)

REPOSITORY = "EvanProgramming/OpenKyrozen"
GATE_NAMES = {
    "R259": "native protocol recovery",
    "R262": "dynamic replanning quality",
    "R263": "matched task success",
    "R264": "matched request and token efficiency",
}


class PublicationUncertain(RuntimeError):
    """GitHub may have published the release; inspection is required before retrying."""


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=600)


def _verify_git_signature(repo: Path, kind: str, revision: str) -> None:
    result = _run(["git", f"verify-{kind}", "--raw", revision], cwd=repo)
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            if len(fields) > 2 and SIGNER_FINGERPRINT in {fields[2], fields[11] if len(fields) > 11 else fields[2]}:
                return
    raise ValueError(f"{kind} signature does not match the pinned release-owner key")


def _verify_candidate_signature(repo: Path, candidate: str) -> None:
    _verify_git_signature(repo, "commit", candidate)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_prepared_artifacts(files: list[Path], expected: dict, attestation: Path) -> None:
    data, _, verified = _read_signed_pair(attestation, attestation.with_name(attestation.name + ".asc"), _gpg_verify)
    try:
        content = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        content = None
    if (not verified or content != expected or
            any(_digest(path) != expected["artifacts"].get(path.name) for path in files)):
        raise ValueError("prepared preview artifacts or attestation changed")


def _require_safe_release_workflow(repo: Path, tag: str) -> None:
    try:
        workflow = yaml.load((repo / ".github/workflows/release.yml").read_text(encoding="utf-8"),
                             Loader=yaml.BaseLoader)
        patterns = workflow["on"]["push"]["tags"]
        condition = workflow["jobs"]["build-release"]["if"]
        if (not tag.startswith("v3.") or patterns != ["v*", "!v3.*"] or
                condition.strip() != "${{ !startsWith(inputs.release_tag || github.ref_name, 'v3.') }}"):
            raise ValueError("ungated release workflow can publish the V3 tag")
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot verify ungated release workflow protection: {exc}") from None


def _ensure_signed_remote_tag(repo: Path, tag: str, candidate: str) -> None:
    local = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=repo, check=False)
    if local.returncode not in (0, 1):
        raise ValueError("preview tag could not be checked locally")
    remote = _run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=repo).stdout.split()
    if local.returncode == 1:
        if remote:
            raise ValueError("preview tag exists on the remote but is not available locally for verification")
        _run(["git", "tag", "-s", "-u", SIGNER_FINGERPRINT, "-m", f"OpenKyrozen {tag}", tag, candidate], cwd=repo)
    _verify_git_signature(repo, "tag", tag)
    if _run(["git", "rev-parse", f"refs/tags/{tag}^{{}}"], cwd=repo).stdout.strip() != candidate:
        raise ValueError("signed preview tag does not point to the candidate")
    tag_object = _run(["git", "rev-parse", f"refs/tags/{tag}"], cwd=repo).stdout.strip()
    if remote and remote[0] != tag_object:
        raise ValueError("remote preview tag does not match the signed local tag")
    if not remote:
        _run(["git", "push", "origin", f"refs/tags/{tag}"], cwd=repo)
    peeled = _run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}^{{}}"], cwd=repo).stdout.split()
    if not peeled or peeled[0] != candidate:
        raise ValueError("remote preview tag does not point to the candidate")


def _notes(candidate: str, manifest: dict, tag: str) -> str:
    lines = [
        "# OpenKyrozen V3 limited-evidence preview", "",
        f"Candidate: `{candidate}`.",
        "Scope: Linux and macOS preview use with `KYROZEN_SANDBOX_REQUIRED=1`.",
        "Windows distribution, production use, and general availability are outside this scope.",
        "The matched V2/V3 evaluation is incomplete or inconclusive; the full V3 benchmark and release gates have not passed.",
        "", "## Install and run", "", "```sh",
        f"curl -fsSL https://raw.githubusercontent.com/{REPOSITORY}/{tag}/install.sh | sh",
        "KYROZEN_SANDBOX_REQUIRED=1 kyrozen", "```",
        "Set `KYROZEN_SANDBOX_REQUIRED=1` for every preview CLI or web launch. If the required sandbox is unavailable, execution is blocked.",
        "", "## Waived gates", "",
    ]
    for gate in manifest["waived_gates"]:
        waiver = manifest["gates"][gate]["waiver"]
        lines.append(f"- {gate} ({GATE_NAMES[gate]}): inconclusive. {waiver['rationale']}")
        lines.extend(f"  - Limitation: {item}" for item in waiver["limitations"])
        lines.append(f"  - Review or expiry: {waiver.get('review_condition') or waiver.get('expires_at')}")
    return "\n".join(lines) + "\n"


def publish(repo: Path, evidence_dir: Path, manifest_path: Path, output_dir: Path | None = None) -> str:
    repo = Path(repo).resolve(strict=True)
    evidence_dir = Path(evidence_dir).resolve(strict=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    _require_outside_checkout(evidence_dir, repo, "evidence directory")
    _require_outside_checkout(manifest_path, repo, "manifest")
    candidate = _current_candidate(repo)
    _verify_candidate_signature(repo, candidate)
    manifest = verify_signed_preview_manifest(evidence_dir, candidate, manifest_path, verify_signature=_gpg_verify)
    if output_dir is None:
        raise ValueError("an outside-checkout output directory is required")
    output_dir = Path(output_dir).resolve()
    _require_outside_checkout(output_dir, repo, "output directory")
    if output_dir.exists():
        raise ValueError("output directory already exists")
    version = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"3\.0\.0\.dev[1-9]\d*", version):
        raise ValueError("candidate package version is not a V3 development preview")
    tag = f"v{version}"
    _require_safe_release_workflow(repo, tag)
    origin = _run(["git", "remote", "get-url", "origin"], cwd=repo).stdout.strip()
    if origin not in {f"https://github.com/{REPOSITORY}.git", f"https://github.com/{REPOSITORY}",
                      f"git@github.com:{REPOSITORY}.git"}:
        raise ValueError("origin does not identify the expected GitHub repository")
    output_dir.mkdir(parents=True)
    staged_manifest = output_dir / "preview-manifest.json"
    shutil.copyfile(manifest_path, staged_manifest)
    shutil.copyfile(manifest_path.with_name(manifest_path.name + ".asc"),
                    staged_manifest.with_name(staged_manifest.name + ".asc"))
    manifest = verify_signed_preview_manifest(evidence_dir, candidate, staged_manifest, verify_signature=_gpg_verify)
    for gate in manifest["waived_gates"]:
        waiver = manifest["gates"][gate]["waiver"]
        for path_key, digest_key in (("record_path", "record_sha256"), ("signature_path", "signature_sha256")):
            source = (evidence_dir / waiver[path_key]).resolve(strict=True)
            source.relative_to(evidence_dir)
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != waiver[digest_key]:
                raise ValueError(f"{gate}: waiver changed during publication preparation")
            (output_dir / source.name).write_bytes(content)
    source_tar = output_dir / "candidate.tar"
    _run(["git", "archive", "--format=tar", f"--output={source_tar}", candidate], cwd=repo)
    source = output_dir / "source"
    source.mkdir()
    with tarfile.open(source_tar) as archive:
        archive.extractall(source, filter="data")
    _run([sys.executable, "-m", "build", "--outdir", str(output_dir)], cwd=source)
    source_tar.unlink()
    shutil.rmtree(source)
    wheel = output_dir / f"openkyrozen-{version}-py3-none-any.whl"
    sdist = output_dir / f"openkyrozen-{version}.tar.gz"
    tui = output_dir / f"openkyrozen-tui-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise ValueError("candidate build did not produce the expected wheel and source archive")
    _run(["git", "archive", "--format=tar.gz", f"--output={tui}", f"{candidate}:tui"], cwd=repo)
    checksum = output_dir / f"{tui.name}.sha256"
    checksum.write_text(f"{_digest(tui)}  {tui.name}\n", encoding="utf-8")
    notes = output_dir / "preview-release-notes.md"
    notes.write_text(_notes(candidate, manifest, tag), encoding="utf-8")
    files = [wheel, sdist, tui, checksum, notes, staged_manifest,
             staged_manifest.with_name(staged_manifest.name + ".asc")]
    for gate in manifest["waived_gates"]:
        waiver = manifest["gates"][gate]["waiver"]
        files.extend(output_dir / Path(waiver[key]).name for key in ("record_path", "signature_path"))
    attestation = output_dir / "preview-artifacts.json"
    artifact_hashes = {path.name: _digest(path) for path in files}
    attestation_data = {"candidate_sha": candidate, "tag": tag, "release_scope": LIMITED_RELEASE_SCOPE,
                        "artifacts": artifact_hashes}
    _write_signed_manifest(attestation_data, attestation)
    files.extend([attestation, attestation.with_name(attestation.name + ".asc")])
    if _current_candidate(repo) != candidate:
        raise ValueError("candidate changed during publication preparation")
    verify_signed_preview_manifest(evidence_dir, candidate, staged_manifest, verify_signature=_gpg_verify)
    _run(["gh", "auth", "status"], cwd=repo)
    _verify_prepared_artifacts(files[:-2], attestation_data, attestation)
    _ensure_signed_remote_tag(repo, tag, candidate)
    _verify_prepared_artifacts(files[:-2], attestation_data, attestation)
    _run(["gh", "release", "create", tag, *map(str, files), "--repo", REPOSITORY,
          "--draft", "--prerelease", "--verify-tag", "--title", f"OpenKyrozen {tag} preview",
          "--notes-file", str(notes)], cwd=repo)
    fields = "tagName,isPrerelease,isDraft,url"
    release = json.loads(_run(["gh", "release", "view", tag, "--repo", REPOSITORY,
                               "--json", fields], cwd=repo).stdout)
    if release.get("tagName") != tag or release.get("isPrerelease") is not True or release.get("isDraft") is not True:
        raise ValueError("GitHub did not confirm the requested draft prerelease")
    expected_uploads = {path.name: _digest(path) for path in files}
    with tempfile.TemporaryDirectory(prefix="v3-preview-download-") as directory:
        download = Path(directory)
        _run(["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", str(download)], cwd=repo)
        if {path.name for path in download.iterdir()} != set(expected_uploads) or any(
                _digest(download / name) != digest for name, digest in expected_uploads.items()):
            raise ValueError("draft release assets do not match the signed local artifacts")
    if _current_candidate(repo) != candidate:
        raise ValueError("candidate changed before publication")
    verify_signed_preview_manifest(evidence_dir, candidate, staged_manifest, verify_signature=_gpg_verify)
    _verify_prepared_artifacts(files[:-2], attestation_data, attestation)
    try:
        _run(["gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--prerelease"], cwd=repo)
        release = json.loads(_run(["gh", "release", "view", tag, "--repo", REPOSITORY,
                                   "--json", fields], cwd=repo).stdout)
        if release.get("tagName") != tag or release.get("isPrerelease") is not True or release.get("isDraft") is not False:
            raise ValueError("GitHub did not confirm the published prerelease")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise PublicationUncertain(f"inspect the GitHub release before retrying: {exc}") from None
    return release["url"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        url = publish(args.repo, args.evidence_dir, args.manifest, args.output_dir)
    except PublicationUncertain as exc:
        print(f"preview publication outcome uncertain: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"preview publication blocked: {exc}", file=sys.stderr)
        return 1
    print(f"Preview prerelease published: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
