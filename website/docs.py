"""Render the repository's public Markdown documentation as static pages."""

from __future__ import annotations

from html import escape, unescape
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import markdown

import build_site as site_builder


ROOT = Path(__file__).resolve().parents[1]
DIST = Path(__file__).resolve().parent / "dist"
BASE = "https://kyrozen.chat"
GITHUB = "https://github.com/EvanProgramming/OpenKyrozen"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML_NS = "http://www.w3.org/1999/xhtml"

ROOT_DOCS = (
    "README.md",
    "README.zh-CN.md",
    "README.ja.md",
    "README.ko.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
)


def source_files() -> list[Path]:
    files = [path for path in (ROOT / "docs").rglob("*.md") if path.is_file()]
    files.extend(ROOT / name for name in ROOT_DOCS if (ROOT / name).is_file())
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix().casefold())


def route_for(source: Path) -> str:
    relative = source.relative_to(ROOT).as_posix()
    if relative == "README.zh-CN.md":
        return "/zh-cn/docs/readme/"
    if relative in ("README.ja.md", "README.ko.md"):
        suffix = "ja" if relative.endswith(".ja.md") else "ko"
        return f"/docs/readme-{suffix}/"
    if relative == "README.md":
        return "/docs/readme/"
    if relative.startswith("docs/"):
        return "/docs/" + relative[5:-3].rstrip("/") + "/"
    return "/docs/" + source.stem.lower().replace("_", "-") + "/"


def plain_text(value: str) -> str:
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_~>#]", "", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def metadata(source: Path, content: str) -> tuple[str, str]:
    headings = []
    for pattern in (r"(?m)^#\s+(.+?)\s*$", r"(?is)<h1\b[^>]*>(.*?)</h1>"):
        match = re.search(pattern, content)
        if match:
            headings.append((match.start(), plain_text(match.group(1))))
    title = min(headings, default=(0, source.stem.replace("-", " ").title()))[1]

    localized_titles = {
        "README.md": "OpenKyrozen README: Local-First Terminal Agent",
        "README.zh-CN.md": "OpenKyrozen 项目介绍（简体中文）",
        "README.ja.md": "OpenKyrozen README（日本語）",
        "README.ko.md": "OpenKyrozen README (한국어)",
    }
    title = localized_titles.get(source.name, title)

    if source.name == "README.md":
        description = "OpenKyrozen is an open-source, local-first terminal agent for coding, research, and project work."
    elif source.name == "README.zh-CN.md":
        description = "OpenKyrozen 是一款开源、本地优先的终端智能体，面向编程、研究与项目工作。"
    else:
        description = ""
        for block in re.split(r"\n\s*\n", content):
            cleaned = plain_text(block)
            if cleaned and not block.lstrip().startswith(("#", "<", "```", "|", "- ", "* ")):
                description = cleaned
                break
        if not description:
            description = f"Read the {title} documentation for OpenKyrozen."
    if len(description) > 175:
        description = description[:172].rsplit(" ", 1)[0] + "…"
    return title, description


def language_for(source: Path) -> tuple[str, str]:
    name = source.name
    if name == "README.zh-CN.md":
        return "zh-CN", "zh-cn"
    if name == "README.ja.md":
        return "ja", "en"
    if name == "README.ko.md":
        return "ko", "en"
    return "en", "en"


def link_map(files: list[Path]) -> dict[Path, str]:
    return {source.resolve(): route_for(source) for source in files}


def rewrite_local_links(rendered: str, source: Path, routes: dict[Path, str]) -> str:
    def rewrite(match: re.Match[str]) -> str:
        attribute, raw = match.group(1), unescape(match.group(2))
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or raw.startswith(("/", "#", "data:")):
            return match.group(0)

        target_path = unquote(parsed.path)
        if not target_path:
            return match.group(0)
        resolved = (source.parent / target_path).resolve()
        route = routes.get(resolved)
        if route:
            destination = route
        else:
            try:
                relative = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                return match.group(0)
            if not resolved.exists():
                destination = GITHUB + "/blob/main/" + quote(relative, safe="/.-_")
            elif attribute == "src":
                destination = "https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/main/" + quote(relative, safe="/.-_")
            else:
                destination = GITHUB + "/blob/main/" + quote(relative, safe="/.-_")

        destination = urlunsplit(("", "", destination, parsed.query, parsed.fragment))
        return f'{attribute}="{escape(destination, quote=True)}"'

    return re.sub(r'\b(href|src)="([^"]+)"', rewrite, rendered)


def category(source: Path) -> str:
    relative = source.relative_to(ROOT).as_posix()
    name = source.stem.lower()
    if relative.startswith("docs/") and any(word in name for word in ("audit", "benchmark", "validation", "roadmap", "refactor")):
        return "Engineering notes and reports"
    if relative.startswith("docs/"):
        return "Product and developer guides"
    if source.name.startswith("README"):
        return "Project overview"
    return "Community and project policies"


def document_entries(files: list[Path]) -> list[dict[str, str]]:
    entries = []
    for source in files:
        text = source.read_text(encoding="utf-8")
        title, description = metadata(source, text)
        lang, _ = language_for(source)
        entries.append({
            "source": str(source),
            "relative": source.relative_to(ROOT).as_posix(),
            "route": route_for(source),
            "title": title,
            "description": description,
            "language": lang,
            "category": category(source),
        })
    return entries


def sync_notice(lang: str, source_url: str) -> str:
    text = {
        "zh-CN": "本页是仓库文档的镜像。GitHub 上的文档可能会先更新；最新内容请查看 GitHub 源文件。",
        "ja": "このページはリポジトリ内ドキュメントのミラーです。GitHub 側が先に更新される場合があるため、最新情報は GitHub の原文をご確認ください。",
        "ko": "이 페이지는 저장소 문서의 미러입니다. GitHub 문서가 먼저 업데이트될 수 있으므로 최신 내용은 GitHub 원문을 확인하세요.",
    }.get(lang, "This page mirrors documentation in the repository. GitHub docs may update first; use the GitHub source for the latest version.")
    label = {"zh-CN": "GitHub 源文件 ↗", "ja": "GitHub 原文 ↗", "ko": "GitHub 원문 ↗"}.get(lang, "GitHub source ↗")
    return f'<aside class="docs-sync-note" role="note"><p>{escape(text)} <a href="{escape(source_url, quote=True)}" rel="noopener noreferrer">{label}</a></p></aside>'


def page_schema(title: str, description: str, canonical: str, lang: str, page_type: str) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": page_type,
        "name": title,
        "description": description,
        "url": canonical,
        "inLanguage": lang,
        "isPartOf": {"@type": "WebSite", "name": "OpenKyrozen", "url": BASE + "/"},
        "publisher": {"@type": "Organization", "name": "OpenKyrozen", "url": GITHUB},
    }
    if page_type == "TechArticle":
        data["headline"] = title
    return '<script type="application/ld+json">' + __import__("json").dumps(data, ensure_ascii=False) + "</script>"


def alternate_links(route: str) -> list[tuple[str, str]]:
    if route == "/docs/":
        return [("en", BASE + "/docs/"), ("zh-CN", BASE + "/zh-cn/docs/"), ("x-default", BASE + "/docs/")]
    if route == "/zh-cn/docs/":
        return [("en", BASE + "/docs/"), ("zh-CN", BASE + "/zh-cn/docs/"), ("x-default", BASE + "/docs/")]
    if route == "/docs/readme/":
        return [("en", BASE + "/docs/readme/"), ("zh-CN", BASE + "/zh-cn/docs/readme/"), ("x-default", BASE + "/docs/readme/")]
    if route == "/zh-cn/docs/readme/":
        return [("en", BASE + "/docs/readme/"), ("zh-CN", BASE + "/zh-cn/docs/readme/"), ("x-default", BASE + "/docs/readme/")]
    return []


def head(title: str, description: str, canonical: str, lang: str, page_type: str, alternates: list[tuple[str, str]]) -> str:
    language_attrs = "\n  ".join(
        f'<link rel="alternate" hreflang="{escape(code, quote=True)}" href="{escape(url, quote=True)}">'
        for code, url in alternates
    )
    og_locale = {"zh-CN": "zh_CN", "ja": "ja_JP", "ko": "ko_KR"}.get(lang, "en_US")
    return f'''<!doctype html>
<html lang="{escape(lang, quote=True)}" class="no-js">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | OpenKyrozen Docs</title>
  <meta name="description" content="{escape(description, quote=True)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <meta property="og:site_name" content="OpenKyrozen">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{escape(title, quote=True)} | OpenKyrozen Docs">
  <meta property="og:description" content="{escape(description, quote=True)}">
  <meta property="og:url" content="{escape(canonical, quote=True)}">
  <meta property="og:locale" content="{og_locale}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{escape(title, quote=True)} | OpenKyrozen Docs">
  <meta name="twitter:description" content="{escape(description, quote=True)}">
  <meta name="theme-color" content="#060A0D">
  <link rel="canonical" href="{escape(canonical, quote=True)}">
  {language_attrs}
  <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
  <link rel="stylesheet" href="/assets/site.css">
  {page_schema(title, description, canonical, lang, page_type)}
  <script type="module" src="/assets/site.js"></script>
</head>'''


def source_url(relative: str) -> str:
    return GITHUB + "/blob/main/" + quote(relative, safe="/.-_")


def render_doc(entry: dict[str, str], routes: dict[Path, str]) -> None:
    source = Path(entry["source"])
    locale, nav_lang = language_for(source)
    rendered = markdown.markdown(
        source.read_text(encoding="utf-8"),
        extensions=["fenced_code", "tables", "toc", "sane_lists", "md_in_html"],
        output_format="html5",
    )
    rendered = rewrite_local_links(rendered, source.resolve(), routes)
    canonical = BASE + entry["route"]
    latest = source_url(entry["relative"])
    content = f'''<body>
  <a class="skip-link" href="#main">Main content</a>
  {site_builder.nav_html(nav_lang, "docs")}
  <main id="main" class="docs-main" aria-label="{escape(entry['title'], quote=True)}">
    <div class="docs-toolbar"><a href="{site_builder.PATHS[nav_lang]['docs']}">← Documentation index</a><span>{escape(entry['relative'])}</span></div>
    {sync_notice(locale, latest)}
    <article class="docs-content">{rendered}</article>
    <p class="docs-source-link"><a href="{escape(latest, quote=True)}" rel="noopener noreferrer">View this file on GitHub ↗</a></p>
  </main>
  <footer class="site-footer">
    <a class="brand" href="{site_builder.PATHS[nav_lang]['home']}"><span class="brand-mark" aria-hidden="true"><i></i><b></b></span><span>openkyrozen<span class="brand-period">.</span></span></a>
    <p>OpenKyrozen documentation · <a href="{GITHUB}">MIT License</a></p>
    <div class="footer-links"><a href="/docs/">Documentation</a><a href="{GITHUB}" target="_blank" rel="noopener noreferrer">GitHub ↗</a></div>
    <span class="footer-note">© OpenKyrozen</span>
  </footer>
</body>
</html>'''
    page_type = "TechArticle" if entry["relative"].startswith("docs/") else "WebPage"
    page = head(entry["title"], entry["description"], canonical, locale, page_type, alternate_links(entry["route"])) + content
    output = DIST / entry["route"].strip("/") / "index.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


def render_index(entries: list[dict[str, str]], lang: str) -> None:
    route = "/docs/" if lang == "en" else "/zh-cn/docs/"
    title = "OpenKyrozen documentation" if lang == "en" else "OpenKyrozen 文档"
    description = (
        "Browse OpenKyrozen product guides, technical references, project policies, and engineering reports. The repository is the freshest source."
        if lang == "en"
        else "浏览 OpenKyrozen 的产品指南、技术参考、项目政策与工程报告。GitHub 仓库是更新最快的来源。"
    )
    if lang == "en":
        intro = "Guides and project notes, published from the OpenKyrozen repository."
        notice = "GitHub documentation may be updated before this site mirror. Open GitHub for the latest repository version."
        notice_link = "Browse the repository ↗"
        aria = "Documentation pages"
        label = "READ THE DOCUMENT"
    else:
        intro = "这些指南和项目文档直接来自 OpenKyrozen 代码仓库。技术文档目前以英文为主。"
        notice = "GitHub 上的文档可能比本站镜像先更新。需要最新内容时，请查看 GitHub 仓库。"
        notice_link = "浏览 GitHub 仓库 ↗"
        aria = "文档页面"
        label = "阅读文档"

    groups: dict[str, list[dict[str, str]]] = {}
    for entry in entries:
        groups.setdefault(entry["category"], []).append(entry)
    blocks = []
    group_order = ("Project overview", "Product and developer guides", "Engineering notes and reports", "Community and project policies")
    for group in group_order:
        if group not in groups:
            continue
        group_label = group
        if lang == "zh-cn":
            group_label = {
                "Project overview": "项目概览",
                "Product and developer guides": "产品与开发指南",
                "Engineering notes and reports": "工程记录与报告",
                "Community and project policies": "社区与项目政策",
            }[group]
        cards = []
        for entry in groups[group]:
            language = "" if entry["language"] in ("en", "zh-CN") else f'<span class="docs-language">{escape(entry["language"].upper())}</span>'
            cards.append(
                f'<a class="docs-card" href="{escape(entry["route"], quote=True)}">'
                f'<span class="docs-card-label">{label} {language}</span>'
                f'<h3>{escape(entry["title"])}</h3><p>{escape(entry["description"])}</p>'
                f'<span class="docs-card-path">{escape(entry["relative"])}</span></a>'
            )
        blocks.append(f'<section class="docs-index-group"><h2>{escape(group_label)}</h2><div class="docs-grid">{"".join(cards)}</div></section>')

    source_index = GITHUB + "/tree/main/docs"
    notice_copy = f'<aside class="docs-sync-note" role="note"><p>{escape(notice)} <a href="{source_index}" rel="noopener noreferrer">{notice_link}</a></p></aside>'
    main = f'''<body>
  <a class="skip-link" href="#main">Main content</a>
  {site_builder.nav_html(lang, "docs")}
  <main id="main" class="docs-main docs-index" aria-label="{escape(aria, quote=True)}">
    <header class="docs-index-heading"><p class="eyebrow"><span class="status-dot" aria-hidden="true"></span>OPENKYROZEN / DOCUMENTATION</p><h1>{escape(title)}</h1><p>{escape(intro)}</p></header>
    {notice_copy}
    {"".join(blocks)}
  </main>
  <footer class="site-footer">
    <a class="brand" href="{site_builder.PATHS[lang]['home']}"><span class="brand-mark" aria-hidden="true"><i></i><b></b></span><span>openkyrozen<span class="brand-period">.</span></span></a>
    <p>OpenKyrozen documentation · <a href="{GITHUB}">MIT License</a></p>
    <div class="footer-links"><a href="{site_builder.PATHS[lang]['docs']}">{site_builder.NAV[lang]['docs']}</a><a href="{GITHUB}" target="_blank" rel="noopener noreferrer">GitHub ↗</a></div>
    <span class="footer-note">© OpenKyrozen</span>
  </footer>
</body>
</html>'''
    canonical = BASE + route
    html = head(title, description, canonical, "en" if lang == "en" else "zh-CN", "CollectionPage", alternate_links(route)) + main
    output = DIST / route.strip("/") / "index.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")


def extend_sitemap(entries: list[dict[str, str]]) -> None:
    ET.register_namespace("", SITEMAP_NS)
    ET.register_namespace("xhtml", XHTML_NS)
    path = DIST / "sitemap.xml"
    tree = ET.parse(path)
    root = tree.getroot()
    existing = {node.text for node in root.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")}
    routes = ["/docs/", "/zh-cn/docs/"] + [entry["route"] for entry in entries]
    for route in routes:
        url = BASE + route
        if url in existing:
            continue
        node = ET.SubElement(root, f"{{{SITEMAP_NS}}}url")
        ET.SubElement(node, f"{{{SITEMAP_NS}}}loc").text = url
        for code, alternate in alternate_links(route):
            link = ET.SubElement(node, f"{{{XHTML_NS}}}link")
            link.set("rel", "alternate")
            link.set("hreflang", code)
            link.set("href", alternate)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    files = source_files()
    entries = document_entries(files)
    routes = link_map(files)
    for entry in entries:
        render_doc(entry, routes)
    render_index(entries, "en")
    render_index(entries, "zh-cn")
    extend_sitemap(entries)
    print(f"Rendered {len(entries)} repository documents and 2 documentation indexes.")


if __name__ == "__main__":
    main()
