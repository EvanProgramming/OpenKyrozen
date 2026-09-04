#!/usr/bin/env python3
"""Run lightweight consistency checks against the live documentation contract."""

from __future__ import annotations

import re
import shlex
import sys
import unittest
from pathlib import Path

from generate_tool_inventory import ROOT, render_inventory, runtime_routes


README_FILES = sorted(ROOT.glob("README*.md"))
VERIFICATION_DOC = ROOT / "docs" / "self-evolution.md"
STALE_TOOL_PATTERNS = (
    re.compile(r"\b26\b[^\n]*(?:tool|工具|ツール|도구)", re.IGNORECASE),
    re.compile(r"(?:Git|git)[^\n]{0,24}\b15\b[^\n]*(?:tool|工具|ツール|도구)", re.IGNORECASE),
    re.compile(r"\b15\b[^\n]*(?:Git|git)[^\n]*(?:tool|工具|ツール|도구)", re.IGNORECASE),
)
STALE_VERIFICATION_CLAIMS = (
    "51d0bce",
    "58 tests",
    "119 tests",
    "359378b",
)
VERIFICATION_COMMANDS = (
    "make check",
    "make docs-check",
    "make shell-check",
    "make lint",
    "make test",
    "make benchmark",
    "git diff --check",
)
BENCHMARK_METADATA = (
    "benchmarks/multi_party_memory.jsonl",
    "openkyrozen-learning-benchmark-v1",
    "deterministic-fixture",
    "openkyrozen-memory-policy-v1",
    "fixture_verified",
    "paired_evidence_status: insufficient",
    "public_superiority_claim_supported: false",
)


def _command_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    active = False
    language = ""
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            if active:
                blocks.extend(current)
                current = []
                active = False
            else:
                language = line[3:].strip().lower()
                active = language in {"bash", "sh", "shell", "console"}
            continue
        if active:
            current.append(line)
    return blocks


def _make_targets() -> set[str]:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z0-9][A-Za-z0-9_-]*):", makefile, re.MULTILINE))


def _route_matches(path: str, routes: set[str]) -> bool:
    normalised = re.sub(r"<[^>]+>", "{id}", path)
    for route in routes:
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", route)
        if re.fullmatch(pattern, normalised):
            return True
    return False


def _check_commands(path: Path, text: str, routes: set[str], targets: set[str]) -> list[str]:
    errors: list[str] = []
    for line_number, line in enumerate(_command_blocks(text), start=1):
        command = line.strip().rstrip("\\").strip()
        if not command or command.startswith("#"):
            continue
        try:
            words = shlex.split(command)
        except ValueError as exc:
            errors.append(f"{path.name}: malformed shell example: {exc}")
            continue
        if words and words[0] == "make" and len(words) > 1:
            target = next((word for word in words[1:] if not word.startswith("-")), "")
            if target and "=" not in target and target not in targets:
                errors.append(f"{path.name}: unknown make target '{target}'")
        if "curl" in words:
            for word in words:
                if not word.startswith(("http://127.0.0.1", "http://localhost")):
                    continue
                url_path = re.sub(r"^https?://[^/]+", "", word).split("?", 1)[0]
                if not _route_matches(url_path, routes):
                    errors.append(f"{path.name}: curl example uses unregistered endpoint '{url_path}'")
    return errors


def _discovered_test_count() -> int:
    suite = unittest.TestLoader().discover(
        str(ROOT / "tests"), pattern="test_*.py"
    )
    return suite.countTestCases()


def _check_verification_record() -> list[str]:
    if not VERIFICATION_DOC.exists():
        return ["docs/self-evolution.md is missing"]
    text = VERIFICATION_DOC.read_text(encoding="utf-8")
    errors: list[str] = []
    for claim in STALE_VERIFICATION_CLAIMS:
        if claim in text:
            errors.append(f"self-evolution.md contains obsolete verification claim '{claim}'")

    if "historical verification snapshot" not in text.lower():
        errors.append("self-evolution.md must label its verification as a historical snapshot")
    if not re.search(r"Historical verification snapshot:\s*`[0-9a-f]{40}`", text):
        errors.append("self-evolution.md must identify a full historical verification commit")

    test_count = _discovered_test_count()
    expected_test_marker = f"Current repository test count at this snapshot: **{test_count} unittest cases**"
    if expected_test_marker not in text:
        errors.append(f"self-evolution.md must record the discovered test count ({test_count})")

    for command in VERIFICATION_COMMANDS:
        if command not in text:
            errors.append(f"self-evolution.md is missing verification command '{command}'")
    for marker in BENCHMARK_METADATA:
        if marker not in text:
            errors.append(f"self-evolution.md is missing benchmark metadata '{marker}'")
    for marker in ("API health/scoping smoke", "CLI command-loop smoke"):
        if marker not in text:
            errors.append(f"self-evolution.md is missing verification evidence '{marker}'")
    return errors


def main() -> int:
    expected_inventory = render_inventory()
    import main as agent_main

    runtime_tool_count = len(agent_main.AVAILABLE_TOOLS)
    git_tool_count = sum(name.startswith("git_") for name in agent_main.AVAILABLE_TOOLS)
    inventory_path = ROOT / "docs" / "tool-inventory.md"
    errors: list[str] = []
    if not inventory_path.exists() or inventory_path.read_text(encoding="utf-8") != expected_inventory:
        errors.append("docs/tool-inventory.md is stale; run scripts/generate_tool_inventory.py --write")

    routes = runtime_routes(__import__("server"))
    route_paths = {path for _, path in routes}
    targets = _make_targets()
    errors.extend(_check_verification_record())
    for readme in README_FILES:
        text = readme.read_text(encoding="utf-8")
        for pattern in STALE_TOOL_PATTERNS:
            match = pattern.search(text)
            if match:
                errors.append(f"{readme.name}: stale tool count: {match.group(0)}")
        if "docs/tool-inventory.md" not in text:
            errors.append(f"{readme.name}: missing link to docs/tool-inventory.md")
        tool_count_lines = [
            line for line in text.splitlines()
            if str(runtime_tool_count) in line and any(marker in line for marker in ("tool", "工具", "ツール", "도구"))
        ]
        if not tool_count_lines:
            errors.append(f"{readme.name}: missing runtime tool count {runtime_tool_count}")
        git_count_lines = [
            line for line in text.splitlines()
            if "Git" in line and str(git_tool_count) in line
            and any(marker in line for marker in ("tool", "工具", "ツール", "도구"))
        ]
        if not git_count_lines:
            errors.append(f"{readme.name}: missing git_ tool count {git_tool_count}")
        for _, route in routes:
            if route not in text:
                errors.append(f"{readme.name}: missing live endpoint {route}")
        errors.extend(_check_commands(readme, text, route_paths, targets))

    if errors:
        print("Documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Documentation check passed for {len(README_FILES)} READMEs and {len(routes)} live endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
