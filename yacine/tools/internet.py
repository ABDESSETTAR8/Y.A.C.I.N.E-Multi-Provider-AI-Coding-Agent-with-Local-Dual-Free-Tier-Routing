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

from ..core.security import SecurityError


def guard_url(url: str) -> None:
    """Reject URLs that point back into the local machine or network.

    Without this the agent can drive its own API (which executes shell
    commands), read the Ollama admin endpoints, reach a cloud metadata service,
    or scan the user's LAN — and every response is fed straight back into the
    prompt, so a malicious page can steer the next tool call.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError(f"Only http/https URLs are allowed (got {parsed.scheme!r}).")
    host = parsed.hostname
    if not host:
        raise SecurityError(f"URL has no host: {url!r}")

    try:
        infos = socket.getaddrinfo(host, parsed.port or
                                   (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SecurityError(f"Could not resolve {host!r}: {e}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise SecurityError(
                f"Refusing to fetch {url!r}: {host} resolves to the non-public "
                f"address {ip}. Internal services are not reachable from tools.")

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

    # Cap the download so a huge or endless response cannot exhaust memory.
    MAX_BYTES = 5 * 1024 * 1024
    MAX_REDIRECTS = 5

    def run(self, url: str, max_chars: int = 8000) -> ToolResult:
        try:
            SecurityEnforcer().check_internet(url)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        try:
            # Redirects are followed MANUALLY so that every hop is re-checked:
            # an allowed public URL can otherwise redirect straight to
            # 127.0.0.1 or a cloud metadata endpoint.
            current = url
            with httpx.Client(timeout=20, follow_redirects=False) as c:
                for _ in range(self.MAX_REDIRECTS + 1):
                    guard_url(current)
                    r = c.get(current, headers={"User-Agent": "YACINE-Agent/1.0"})
                    if r.is_redirect and r.headers.get("location"):
                        current = str(httpx.URL(current).join(r.headers["location"]))
                        continue
                    break
                else:
                    return ToolResult(ok=False, output="Too many redirects.")
                r.raise_for_status()
                raw = r.content[: self.MAX_BYTES]
                text = raw.decode(r.encoding or "utf-8", errors="ignore")
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        except Exception as e:
            return ToolResult(ok=False, output=f"Request failed: {e}")
        # Strip HTML tags crudely to get readable text.
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return ToolResult(ok=True, output=text[:max_chars],
                          data={"status": r.status_code, "url": current})


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
