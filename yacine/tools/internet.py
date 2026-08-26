"""HTTP / internet research tool (gated by internet_access permission).

Per the spec the Research Agent searches Google/GitHub/MDN/PyPI/npm/StackOverflow
etc. This tool performs real HTTP GETs (with a search-engine-friendly query),
returns text, and trims noise. It is the substrate for the internet research
pillar. WebSearch/WebFetch tool-calling can also sit on top of this.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

import httpx

from .base import Tool, ToolResult
from ..core.security import SecurityEnforcer


class HttpRequestTool(Tool):
    name = "http_request"
    description = "Perform an HTTP GET request to a URL and return the response text (truncated)."
    needs_internet = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL to fetch."},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)."},
        },
        "required": ["url"],
    }

    def run(self, url: str, max_chars: int = 8000) -> ToolResult:
        SecurityEnforcer().check_internet(url)
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as c:
                r = c.get(url, headers={"User-Agent": "YACINE-Agent/1.0"})
                r.raise_for_status()
                text = r.text
        except Exception as e:
            return ToolResult(ok=False, output=f"Request failed: {e}")
        # Strip HTML tags crudely to get readable text.
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return ToolResult(ok=True, output=text[:max_chars],
                          data={"status": 200, "url": url})


class WebSearchTool(Tool):
    name = "web_search"
    description = ("Search the web for a query and return a list of result snippets/URLs. "
                   "Sources are ranked by reliability heuristics (official docs first).")
    needs_internet = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "site": {"type": "string", "description": "Optional site filter, e.g. 'docs.python.org' or 'pypi.org'."},
        },
        "required": ["query"],
    }

    # Domains treated as high-reliability documentation sources.
    TRUSTED = ["docs.", "github.com", "pypi.org", "npmjs.com", "stackoverflow.com",
               "developer.mozilla.org", "learn.microsoft.com", "rust-lang.org",
               "kotlinlang.org", "docs.oracle.com", "huggingface.co", "docker.com"]

    def run(self, query: str, site: str = "") -> ToolResult:
        SecurityEnforcer().check_internet()
        q = query
        if site:
            q = f"site:{site} {query}"
        url = f"https://duckduckgo.com/html/?q={quote_plus(q)}"
        http = HttpRequestTool()
        res = http.run(url, max_chars=6000)
        if not res.ok:
            return res
        # Extract result links + snippets crudely.
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', res.output, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', res.output, re.S)
        out = []
        for i, (href, title) in enumerate(links[:8]):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]) if i < len(snippets) else ""
            title = re.sub(r"<[^>]+>", "", title)
            out.append(f"{i+1}. {title.strip()}\n   {href}\n   {snippet.strip()[:200]}")
        ranked = self._rank(out)
        return ToolResult(ok=True, output="\n".join(ranked) or res.output,
                          data={"query": query})

    def _rank(self, results):
        def score(r: str) -> int:
            s = 0
            low = r.lower()
            if any(t in low for t in self.TRUSTED):
                s += 5
            return s
        return sorted(results, key=score, reverse=True)
