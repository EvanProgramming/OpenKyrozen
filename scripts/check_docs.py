#!/usr/bin/env python3
"""Run lightweight consistency checks against the live documentation contract."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from generate_tool_inventory import ROOT, render_inventory, runtime_routes, _load_runtime


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
STALE_DEEPSEEK_MODELS = re.compile(r"\bdeepseek-(?:chat|reasoner)\b", re.IGNORECASE)
LINK_PATTERN = re.compile(r"!?\[([^\]]*)\]\((<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
PUBLIC_MARKDOWN = ("README*.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md")


def markdown_links(text: str) -> list[tuple[int, str]]:
    """Return inline Markdown link targets outside fenced code blocks."""
    text = re.sub(r"(?ms)^\s*(```+|~~~+).*?^\s*\1\s*$", "", text)
    return [
        (text.count("\n", 0, match.start()) + 1, match.group(2).strip("<>"))
        for match in LINK_PATTERN.finditer(text)
    ]


def anchor_for_heading(heading: str) -> str:
    """Create the GitHub-style fragment used by a Markdown heading."""
    heading = re.sub(r"[`*_~]", "", heading).casefold().strip()
    heading = re.sub(r"[^\w\-\s]", "", heading, flags=re.UNICODE)
    return re.sub(r"\s+", "-", heading)


def _heading_anchors(text: str) -> set[str]:
    headings: set[str] = set()
    counts: dict[str, int] = {}
    in_code = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_code = not in_code
            continue
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match and not in_code:
            slug = anchor_for_heading(match.group(1))
            count = counts.get(slug, 0)
            headings.add(slug if not count else f"{slug}-{count}")
            counts[slug] = count + 1
    return headings


def check_markdown_links(source: Path, root: Path) -> list[str]:
    """Validate local Markdown paths and fragments, with source line numbers."""
    text = source.read_text(encoding="utf-8")
    errors: list[str] = []
    for line, raw in markdown_links(text):
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or raw.startswith(("#", "mailto:")):
            if raw.startswith("#") and unquote(raw[1:]) not in _heading_anchors(text):
                errors.append(f"{source.name}:{line}: missing anchor '{raw[1:]}'")
            continue
        target = (source.parent / unquote(parsed.path)).resolve()
        if not target.exists():
            errors.append(f"{source.name}:{line}: missing link target '{parsed.path}'")
            continue
        if parsed.fragment and target.suffix.lower() == ".md":
            anchors = _heading_anchors(target.read_text(encoding="utf-8"))
            if unquote(parsed.fragment) not in anchors:
                errors.append(f"{source.name}:{line}: missing anchor '{parsed.fragment}' in '{parsed.path}'")
        try:
            target.relative_to(root.resolve())
        except ValueError:
            errors.append(f"{source.name}:{line}: link escapes the repository '{parsed.path}'")
    return errors


def _check_index_coverage(docs_root: Path) -> list[str]:
    index_path = docs_root / "index.md"
    if not index_path.exists():
        return ["docs/index.md is missing"]
    index_text = index_path.read_text(encoding="utf-8")
    indexed = {
        (index_path.parent / unquote(urlsplit(target).path)).resolve()
        for _, target in markdown_links(index_text)
        if not urlsplit(target).scheme and not urlsplit(target).netloc and urlsplit(target).path
    }
    return [
        f"docs/index.md: missing index entry for '{path.relative_to(docs_root.parent).as_posix()}'"
        for path in sorted(docs_root.rglob("*.md"))
        if path != index_path and path.resolve() not in indexed
    ]


def _check_release_references(readmes: list[Path], website_sources: list[Path]) -> list[str]:
    """Reject drift between release commands shown on the README and website."""
    versions: dict[str, set[str]] = {}
    for source in [*readmes, *website_sources]:
        text = source.read_text(encoding="utf-8")
        found = set(re.findall(r"/v(\d+\.\d+\.\d+)(?:/|\b)", text))
        if found:
            versions[source.name] = found
    all_versions = set().union(*versions.values()) if versions else set()
    errors = []
    if len(all_versions) != 1:
        errors.append(f"public installer references disagree on release version: {sorted(all_versions)}")
    for name, found in versions.items():
        if len(found) != 1 or found != all_versions:
            errors.append(f"{name}: installer release version {sorted(found)} does not match the public release")
    for source in [*readmes, *website_sources]:
        if source.name not in versions:
            errors.append(f"{source.name}: missing versioned installer reference")
    return errors


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


def _check_provider_defaults(path: Path, text: str, defaults: dict[str, tuple[str, str]]) -> list[str]:
    """Keep README model examples tied to the provider's current defaults."""
    simple, complex_model = defaults["deepseek"]
    errors: list[str] = []
    if STALE_DEEPSEEK_MODELS.search(text):
        errors.append(f"{path.name}: README contains a retired DeepSeek model name")
    simple_match = re.search(r'(?:KYROZEN_MODEL_SIMPLE\s*=\s*|"model_simple"\s*:\s*")([^"\s]+)', text)
    complex_match = re.search(r'(?:KYROZEN_MODEL_COMPLEX\s*=\s*|"model_complex"\s*:\s*")([^"\s]+)', text)
    if not simple_match or simple_match.group(1) != simple:
        errors.append(f"{path.name}: model_simple example must match provider default '{simple}'")
    if not complex_match or complex_match.group(1) != complex_model:
        errors.append(f"{path.name}: model_complex example must match provider default '{complex_model}'")
    return errors


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

    return errors


def main() -> int:
    expected_inventory = render_inventory()
    tools, server, _ = _load_runtime()
    from openkyrozen.providers import PROVIDER_DEFAULT_MODELS

    runtime_tool_count = len(tools)
    git_tool_count = sum(name.startswith("git_") for name in tools)
    inventory_path = ROOT / "docs" / "tool-inventory.md"
    errors: list[str] = []
    if not inventory_path.exists() or inventory_path.read_text(encoding="utf-8") != expected_inventory:
        errors.append("docs/tool-inventory.md is stale; run scripts/generate_tool_inventory.py --write")

    routes = runtime_routes(server)
    route_paths = {path for _, path in routes}
    targets = _make_targets()
    errors.extend(_check_verification_record())
    public_docs = [ROOT / name for pattern in PUBLIC_MARKDOWN for name in sorted(path.name for path in ROOT.glob(pattern))]
    public_docs.extend(path for path in (ROOT / "docs").rglob("*.md") if path.is_file())
    for document in sorted(set(public_docs)):
        errors.extend(check_markdown_links(document, ROOT))

    errors.extend(_check_index_coverage(ROOT / "docs"))
    errors.extend(_check_release_references(
        README_FILES,
        [ROOT / "website" / "build_site.py", ROOT / "website" / "assets" / "install-platform.js"],
    ))

    defaults_doc = ROOT / "docs" / "configuration.md"
    errors.extend(_check_provider_defaults(defaults_doc, defaults_doc.read_text(encoding="utf-8"), PROVIDER_DEFAULT_MODELS))
    for readme in README_FILES:
        text = readme.read_text(encoding="utf-8")
        for pattern in STALE_TOOL_PATTERNS:
            match = pattern.search(text)
            if match:
                errors.append(f"{readme.name}: stale tool count: {match.group(0)}")
        if "docs/index.md" not in text:
            errors.append(f"{readme.name}: missing link to docs/index.md")
        if "docs/tool-inventory.md" not in text:
            errors.append(f"{readme.name}: missing link to docs/tool-inventory.md")
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
