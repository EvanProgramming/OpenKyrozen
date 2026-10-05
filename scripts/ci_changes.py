#!/usr/bin/env python3
"""Select CI jobs from the complete set of changed paths."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess


CHECKS = ("python", "tui", "wheel", "docker", "updates", "docs")
ALL_CHECKS = frozenset({"all"})

DOC_FILES = {
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/labels.yml",
}
CI_FILES = {"Makefile", ".github/workflows/ci.yml", "scripts/ci_changes.py"}
PACKAGE_FILES = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "MANIFEST.in",
    "agent.yaml",
    "requirements.txt",
}
DOCKER_FILES = {"Dockerfile", ".dockerignore", "requirements.txt"}
WHEEL_SCRIPTS = {
    "scripts/wheel_smoke.py",
    "scripts/tui_workflow_acceptance.py",
}
UPDATE_SCRIPTS = {"scripts/update_workflow_acceptance.py"}
UPDATE_FILES = {
    "install.sh",
    "install.ps1",
    "tui/go.mod",
    "tui/go.sum",
    "pyproject.toml",
    "requirements.txt",
    "agent.yaml",
}
UPDATE_PREFIXES = (
    "openkyrozen/updates/",
    "openkyrozen/interfaces/cli/",
    "openkyrozen/app/",
    "openkyrozen/persistence/",
    "openkyrozen/memory/",
    "openkyrozen/tasks/",
)
PACKAGE_PREFIXES = ("openkyrozen/", "prompts/", "plugins/", "builtin_skills/")
PYTHON_PREFIXES = (
    "openkyrozen/",
    "tests/",
    "scripts/",
    "prompts/",
    "plugins/",
    "builtin_skills/",
    "benchmarks/",
)


def classify_paths(paths: set[str] | list[str] | tuple[str, ...]) -> set[str]:
    """Return required checks; unknown paths conservatively select everything."""
    selected: set[str] = set()
    normalized = {PurePosixPath(path).as_posix().lstrip("./") for path in paths}
    for path in normalized:
        name = PurePosixPath(path).name
        if path in CI_FILES:
            return set(ALL_CHECKS)

        if (
            path.startswith("docs/")
            or path.startswith("website/")
            or path.startswith(".github/ISSUE_TEMPLATE/")
            or path in DOC_FILES
            or (name.startswith("README") and name.endswith(".md"))
        ):
            selected.add("docs")
        elif path.startswith(".github/"):
            return set(ALL_CHECKS)

        if path in PACKAGE_FILES or name.startswith("requirements") and name.endswith(".txt") or path.startswith(PACKAGE_PREFIXES):
            selected.update(("wheel", "docker"))
            if path in PACKAGE_FILES or name.startswith("requirements"):
                selected.add("python")
        if path.startswith("openkyrozen/") or path in {
            "main.py", "server.py", "tui_backend.py", "tui_launcher.py",
            "learning_worker.py", "github_cli.py", "main_debug.py",
            "migration.py", "learning_benchmark.py",
        }:
            selected.add("python")
            selected.add("docker")
        if path.startswith(PYTHON_PREFIXES) or (
            "/" not in path and path.endswith(".py")
        ):
            selected.add("python")
        if path in UPDATE_FILES or path.startswith(UPDATE_PREFIXES):
            selected.add("updates")
        if path in UPDATE_SCRIPTS:
            selected.add("updates")
        if path in WHEEL_SCRIPTS:
            selected.add("wheel")
        if path.startswith("tui/"):
            selected.update(("tui", "wheel", "updates"))
        if path in DOCKER_FILES or path == "scripts/docker_persistence_smoke.sh":
            selected.add("docker")

        recognized = (
            path in DOC_FILES
            or path.startswith(("docs/", "website/", ".github/ISSUE_TEMPLATE/"))
            or (name.startswith("README") and name.endswith(".md"))
            or path in CI_FILES
            or path in PACKAGE_FILES
            or (name.startswith("requirements") and name.endswith(".txt"))
            or path.startswith(PACKAGE_PREFIXES)
            or path.startswith(PYTHON_PREFIXES)
            or path in DOCKER_FILES
            or path == "scripts/docker_persistence_smoke.sh"
            or path in UPDATE_FILES
            or path in UPDATE_SCRIPTS
            or path in WHEEL_SCRIPTS
            or path.startswith(UPDATE_PREFIXES)
            or path.startswith("tui/")
            or path in {
                "main.py", "server.py", "tui_backend.py", "tui_launcher.py",
                "learning_worker.py", "github_cli.py", "main_debug.py",
                "migration.py", "learning_benchmark.py",
            }
            or ("/" not in path and path.endswith(".py"))
        )
        if not recognized:
            return set(ALL_CHECKS)

    return selected


def changed_paths(root: Path, event: str, base: str, head: str) -> set[str] | None:
    """Read all changed paths, including both sides of renames; None means full CI."""
    if not base or not head or set(base) == {"0"}:
        return None
    revision = f"{base}...{head}" if event == "pull_request" else f"{base}..{head}"
    try:
        result = subprocess.run(
            ["git", "diff", "--find-renames", "--name-status", "-z", revision],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    fields = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        count = 2 if status.startswith("R") or status.startswith("C") else 1
        paths.update(fields[index : index + count])
        index += count
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    paths = None if args.full else changed_paths(Path.cwd(), args.event, args.base, args.head)
    checks = set(ALL_CHECKS) if paths is None else classify_paths(paths)
    outputs = {name: str(name in checks or "all" in checks).lower() for name in CHECKS}
    outputs["all"] = str("all" in checks).lower()
    outputs["core"] = str(bool(checks & {"python", "docs"})).lower()
    outputs["full"] = str(args.full or "all" in checks).lower()

    output_path = os.environ.get("GITHUB_OUTPUT")
    rendered = "".join(f"{name}={value}\n" for name, value in outputs.items())
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            stream.write(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
