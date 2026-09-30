from __future__ import annotations

import json
import re
import requests

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def search_web(self, args: str) -> str:
    """
    Search the internet for real-time information.
    Args format: "query" (e.g., "latest bitcoin price", "who won the super bowl").
    Returns Title + Snippet for top 5 results. Use for current events, prices, or unknown facts.
    """
    import re
    import html
    import json
    from urllib.parse import quote
    import requests

    query = (args or "").strip()
    if not query:
        return "Search Error: query is empty."

    def _fmt(results):
        lines = []
        for r in results:
            title = r.get("title", "")
            snippet = r.get("body", "")
            url = r.get("url", "")
            line = f"- Title: {title}"
            if snippet:
                line += f"\n  Snippet: {snippet}"
            if url:
                line += f"\n  URL: {url}"
            lines.append(line)
        return "\n".join(lines)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.6943.126 Safari/537.36"
        ),
    }

    # ---- attempt 1: Google via googlesearch-python (no API key) ----
    try:
        from googlesearch import search as google_search
        urls = list(google_search(query, num_results=5, lang="en"))
    except Exception:
        pass
    else:
        if urls:
            results = []
            for url in urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=5)
                    resp.raise_for_status()
                except requests.RequestException:
                    results.append({"title": url, "body": "", "url": url})
                    continue
                title = ""
                m = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.DOTALL|re.IGNORECASE)
                if m:
                    title = html.unescape(re.sub(r"<[^>]*>", "", m.group(1))).strip()
                snippet = ""
                m2 = re.search(
                    r'<meta\s+name\s*=\s*["\']description["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
                    resp.text, re.IGNORECASE
                )
                if m2:
                    snippet = html.unescape(m2.group(1)).strip()
                results.append({"title": title, "body": snippet, "url": url})
            if results:
                return _fmt(results)

    # ---- attempt 2: DuckDuckGo Lite HTML (GET) ----
    lite_url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
    try:
        resp = requests.get(lite_url, headers=headers, timeout=10)
        resp.raise_for_status()
        titles = re.findall(r'<a[^>]*rel="nofollow"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        snippets = re.findall(r'<td class="result-snippet"[^>]*>(.*?)</td>', resp.text, re.DOTALL)
        if titles:
            results = []
            for i, title_html in enumerate(titles[:5]):
                title = re.sub(r"<[^>]*>", "", title_html).strip()
                title = html.unescape(title)
                snippet = ""
                if i < len(snippets):
                    snip_html = snippets[i]
                    snippet = re.sub(r"<[^>]*>", "", snip_html).strip()
                    snippet = html.unescape(snippet)
                results.append({"title": title, "body": snippet, "url": ""})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 3: DuckDuckGo HTML (POST) ----
    html_url = "https://html.duckduckgo.com/html/"
    try:
        resp = requests.post(html_url, data={"q": query}, headers=headers, timeout=10)
        resp.raise_for_status()
        titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        if titles:
            results = []
            for i, title_html in enumerate(titles[:5]):
                title = re.sub(r"<[^>]*>", "", title_html).strip()
                title = html.unescape(title)
                snippet = ""
                if i < len(snippets):
                    snip_html = snippets[i]
                    snippet = re.sub(r"<[^>]*>", "", snip_html).strip()
                    snippet = html.unescape(snippet)
                results.append({"title": title, "body": snippet, "url": ""})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 4: DuckDuckGo Instant Answer API (JSON) ----
    try:
        api_url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
        resp = requests.get(api_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
        related = data.get("RelatedTopics", [])
        if related:
            results = []
            for topic in related[:5]:
                title = topic.get("Text", "") or ""
                url = topic.get("FirstURL", "")
                # Clean up title (remove description suffix)
                if " - " in title:
                    title = title.split(" - ")[0]
                results.append({"title": title[:200], "body": "", "url": url})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 5: direct Wikipedia search for fact-based queries ----
    try:
        wiki_url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={quote(query)}&limit=5&format=json"
        resp = requests.get(wiki_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
        if len(data) >= 4 and data[1]:
            results = []
            for i, title in enumerate(data[1][:5]):
                desc = data[2][i] if i < len(data[2]) else ""
                url = data[3][i] if i < len(data[3]) else ""
                results.append({"title": title, "body": desc[:300], "url": url})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # All backends failed — return a useful error message
    return (
        "Search temporarily unavailable: all backends (Google, DuckDuckGo, Wikipedia) "
        f"failed for query '{query[:80]}'. This may be due to rate-limiting or "
        "network issues. Suggestions: try a shorter query, wait a few seconds and "
        "retry, or use read_webpage on a known URL instead."
    )
