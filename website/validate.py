"""Fail the website build when crawl-critical metadata or links regress."""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


DIST = Path(__file__).resolve().parent / "dist"
BASE = "https://kyrozen.chat"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.in_json_ld = False
        self.title = ""
        self.lang = ""
        self.canonicals: list[str] = []
        self.descriptions: list[str] = []
        self.alternates: dict[str, str] = {}
        self.links: list[str] = []
        self.ids: set[str] = set()
        self.h1_count = 0
        self.json_ld = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.lang = values.get("lang") or ""
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            self.h1_count += 1
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "meta" and values.get("name") == "description":
            self.descriptions.append(values.get("content") or "")
        if tag == "link" and values.get("rel") == "canonical":
            self.canonicals.append(values.get("href") or "")
        if tag == "link" and values.get("rel") == "alternate" and values.get("hreflang"):
            self.alternates[values["hreflang"] or ""] = values.get("href") or ""
        if tag in {"a", "img", "script", "link"}:
            for attribute in ("href", "src"):
                if values.get(attribute):
                    self.links.append(values[attribute] or "")
        if tag == "script" and values.get("type") == "application/ld+json":
            self.in_json_ld = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "script":
            self.in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self.in_json_ld:
            self.json_ld += data


def route_for(path: Path) -> str:
    relative = path.relative_to(DIST).as_posix()
    if relative == "index.html":
        return "/"
    if relative.endswith("/index.html"):
        return "/" + relative.removesuffix("index.html")
    return "/" + relative


def check() -> None:
    errors: list[str] = []
    html_pages = sorted(DIST.rglob("*.html"))
    pages: dict[Path, Page] = {}
    for path in html_pages:
        page = Page()
        page.feed(path.read_text(encoding="utf-8"))
        pages[path] = page
        route = route_for(path)
        expected = BASE + route
        if not page.title.strip():
            errors.append(f"Missing title: {route}")
        if len(page.descriptions) != 1 or not page.descriptions[0].strip():
            errors.append(f"Missing or duplicate description: {route}")
        if page.canonicals != [expected]:
            errors.append(f"Canonical mismatch: {route} -> {page.canonicals}")
        if not page.lang:
            errors.append(f"Missing document language: {route}")
        if page.h1_count < 1:
            errors.append(f"Missing H1: {route}")
        if page.json_ld:
            try:
                json.loads(page.json_ld)
            except json.JSONDecodeError:
                errors.append(f"Invalid JSON-LD: {route}")
        else:
            errors.append(f"Missing structured data: {route}")
        if route.startswith("/docs/") or route.startswith("/zh-cn/docs/"):
            source_note = path.read_text(encoding="utf-8")
            if "docs-sync-note" not in source_note:
                errors.append(f"Missing GitHub freshness notice: {route}")

    titles = Counter(page.title.strip() for page in pages.values())
    errors.extend(f"Duplicate title: {title}" for title, count in titles.items() if count > 1)

    for path, page in pages.items():
        source_route = route_for(path)
        for language, alternate in page.alternates.items():
            target = urlsplit(alternate)
            if target.scheme != "https" or target.netloc != "kyrozen.chat":
                errors.append(f"Invalid hreflang target on {source_route}: {alternate}")
                continue
            destination = route_path(target.path)
            if destination not in pages:
                errors.append(f"Missing hreflang page on {source_route}: {alternate}")
                continue
            if language != "x-default" and BASE + source_route not in pages[destination].alternates.values():
                errors.append(f"Non-reciprocal hreflang on {source_route}: {alternate}")

        for href in page.links:
            url = urlsplit(href)
            if url.scheme or url.netloc or not url.path.startswith("/"):
                continue
            destination = route_path(url.path)
            if destination is None:
                errors.append(f"Missing internal link from {source_route}: {href}")
            elif url.fragment and destination in pages and url.fragment not in pages[destination].ids:
                errors.append(f"Missing internal anchor from {source_route}: {href}")

    sitemap_file = DIST / "sitemap.xml"
    try:
        sitemap = ET.parse(sitemap_file).getroot()
        locations = [node.text or "" for node in sitemap.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")]
    except (ET.ParseError, FileNotFoundError):
        locations = []
        errors.append("Missing or invalid sitemap.xml")
    expected_locations = {BASE + route_for(path) for path in html_pages}
    if len(locations) != len(set(locations)):
        errors.append("Duplicate URLs in sitemap.xml")
    if set(locations) != expected_locations:
        errors.append("Sitemap URLs do not match generated HTML routes")

    robots_file = DIST / "robots.txt"
    robots = robots_file.read_text(encoding="utf-8") if robots_file.is_file() else ""
    if "User-agent: *" not in robots or "Allow: /" not in robots or f"Sitemap: {BASE}/sitemap.xml" not in robots:
        errors.append("robots.txt must allow crawling and point to the sitemap")

    if errors:
        raise SystemExit("SEO validation failed:\n- " + "\n- ".join(errors))
    print(f"SEO validation passed for {len(html_pages)} pages and {len(locations)} sitemap URLs.")


def route_path(path: str) -> Path | None:
    if not path or path.endswith("/"):
        candidate = DIST / path.lstrip("/") / "index.html" if path else DIST / "index.html"
    else:
        candidate = DIST / path.lstrip("/")
        if not candidate.suffix:
            candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


if __name__ == "__main__":
    check()
