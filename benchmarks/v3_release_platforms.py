"""Collect six-platform CI artifacts into an unsigned R266 evidence record."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_release_gates import REQUIRED_PLATFORMS

_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_artifact(source_root: Path, staging: Path, platform: str, relative: object) -> str:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"{platform}: evidence artifact path must be relative")
    source = (source_root / relative).resolve()
    try:
        source.relative_to(source_root)
    except ValueError:
        raise ValueError(f"{platform}: evidence artifact escapes its result directory") from None
    if not source.is_file():
        raise ValueError(f"{platform}: evidence artifact is missing or not a file: {relative}")
    destination_relative = (Path("platforms") / platform / relative).as_posix()
    destination = staging / destination_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination_relative


def _matches_platform(platform: str, environment: dict) -> bool:
    os_name, python_id = platform.rsplit("-", 1)
    python_version = f"3.{python_id[-2:]}"
    actual_os = environment["os"].lower()
    os_matches = {
        "linux": "linux" in actual_os,
        "macos": "mac" in actual_os or "darwin" in actual_os,
        "windows": "windows" in actual_os,
    }[os_name]
    return os_matches and environment["python"].startswith(python_version)


def collect_platform_evidence(results_dir: Path, output_dir: Path, candidate_sha: str,
                              command_set: Path) -> dict:
    """Build an unsigned, candidate-bound R266.json bundle from passing matrix artifacts."""
    results_dir = Path(results_dir).resolve(strict=True)
    command_set = Path(command_set).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if not _GIT_SHA.fullmatch(candidate_sha):
        raise ValueError("candidate SHA must be a full Git object ID")
    if output_dir.exists():
        raise ValueError("evidence output directory already exists")
    if not command_set.is_file():
        raise ValueError("command set must be a regular file")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    found_platforms = {path.name.removeprefix("v3-platform-") for path in results_dir.iterdir()
                       if path.name.startswith("v3-platform-")}
    if found_platforms != set(REQUIRED_PLATFORMS):
        raise ValueError("results directory must contain exactly the six required platform artifacts")

    with tempfile.TemporaryDirectory(prefix="v3-r266-", dir=output_dir.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        command_path = staging / "commands" / command_set.name
        command_path.parent.mkdir(parents=True)
        shutil.copyfile(command_set, command_path)
        fixture_path = staging / "fixtures" / "v3-fixtures.json"
        fixture_path.parent.mkdir(parents=True)
        fixture_path.write_text(json.dumps(FIXTURES, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        evidence = []
        platform_results = []
        dependency_sets = {}
        for platform in REQUIRED_PLATFORMS:
            result_dir = results_dir / f"v3-platform-{platform}"
            result_path = result_dir / "platform.json"
            if result_dir.is_symlink() or not result_dir.is_dir() or result_path.is_symlink():
                raise ValueError(f"{platform}: platform result directory or metadata cannot be a symlink")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise ValueError(f"{platform}: missing or invalid platform result") from None
            if not isinstance(result, dict) or result.get("schema_version") != 1:
                raise ValueError(f"{platform}: unsupported platform result schema")
            if result.get("candidate_sha") != candidate_sha or result.get("platform") != platform:
                raise ValueError(f"{platform}: candidate SHA or platform does not match")
            if platform.startswith("windows-"):
                if (result.get("unit") != "not_run" or result.get("integration") != "not_run"
                        or result.get("supported_features") != "passed"):
                    raise ValueError(f"{platform}: supported-feature acceptance did not pass")
            elif any(result.get(check) != "passed" for check in ("unit", "integration")):
                raise ValueError(f"{platform}: platform validation did not pass (unit/integration)")
            if result.get("acceptance") != "passed":
                raise ValueError(f"{platform}: platform validation did not pass (acceptance)")
            soak_seconds = result.get("soak_seconds")
            if isinstance(soak_seconds, bool) or not isinstance(soak_seconds, int) or soak_seconds < 1800:
                raise ValueError(f"{platform}: 30-minute soak has not passed")
            environment = result.get("environment")
            dependencies = result.get("dependencies")
            if not isinstance(environment, dict) or any(
                    not isinstance(environment.get(key), str) or not environment[key]
                    for key in ("runner", "os", "python")):
                raise ValueError(f"{platform}: runner, OS, and Python details are required")
            if not isinstance(dependencies, dict) or not dependencies:
                raise ValueError(f"{platform}: dependency inventory must be an object")
            paths = result.get("evidence_paths")
            if (not isinstance(paths, list) or not paths
                    or any(not isinstance(path, str) for path in paths)
                    or len(paths) != len(set(paths)) or "platform.json" in paths):
                raise ValueError(f"{platform}: evidence artifact list is required")
            if not _matches_platform(platform, environment):
                raise ValueError(f"{platform}: runner OS or Python version does not match")
            copied_paths = [_copy_artifact(result_dir, staging, platform, "platform.json")]
            copied_paths.extend(_copy_artifact(result_dir, staging, platform, path) for path in paths)
            for relative in copied_paths:
                evidence.append({"path": relative, "sha256": _sha256(staging / relative)})
            dependency_sets[platform] = dependencies
            platform_results.append({
                "platform": platform,
                "unit": result["unit"],
                "integration": result["integration"],
                "acceptance": "passed",
                "soak_seconds": soak_seconds,
                "evidence_paths": copied_paths,
                "environment": environment,
                "dependencies": dependencies,
                **({"supported_features": "passed"} if platform.startswith("windows-") else {}),
            })

        command_relative = (Path("commands") / command_set.name).as_posix()
        fixture_relative = "fixtures/v3-fixtures.json"
        record = {
            "schema_version": 1,
            "gate_id": "R266",
            "candidate_sha": candidate_sha,
            "command_set_path": command_relative,
            "command_set_sha256": _sha256(staging / command_relative),
            "fixture_set_path": fixture_relative,
            "fixture_set_sha256": _sha256(staging / fixture_relative),
            "environment": {"runner": "GitHub Actions matrix", "os": "Linux, macOS, Windows",
                            "python": "3.12 and 3.13"},
            "dependencies": {"platforms": dependency_sets},
            "result": "passed",
            "failure_class": None,
            "evidence": evidence,
            "platform_results": platform_results,
        }
        record_path = staging / "R266.json"
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.mkdir(output_dir)
        except FileExistsError:
            raise ValueError("evidence output directory already exists") from None
        try:
            for item in staging.iterdir():
                os.rename(item, output_dir / item.name)
        except OSError:
            shutil.rmtree(output_dir)
            raise
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--command-set", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record = collect_platform_evidence(args.results_dir, args.output_dir,
                                           args.candidate_sha, args.command_set)
    except (OSError, ValueError) as exc:
        print(f"R266 evidence collection blocked: {exc}", file=sys.stderr)
        return 1
    print(f"collected unsigned R266 evidence for {record['candidate_sha']} at {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
