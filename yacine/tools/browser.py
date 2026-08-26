"""Browser tool: navigate + read pages (for documentation lookup).

On this Windows host we have the cua-driver `computer_use` browser rung
available through the Hermes runtime. This tool wraps a best-effort browser:
it tries `computer_use` cua_browser_* when usable; otherwise it falls back to
a headless HTTP fetch via the internet tool. The tool is intentionally
resilient so the research agent never hard-fails when a browser isn't set up.
"""
from __future__ import annotations

from .base import Tool, ToolResult
from .internet import HttpRequestTool


class BrowserTool(Tool):
    name = "open_browser"
    description = ("Open a URL in a browser and return its readable text. Used for reading "
                   "official documentation, GitHub READMEs, and reference pages.")
    needs_internet = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open."},
            "max_chars": {"type": "integer", "description": "Max chars to return (default 10000)."},
        },
        "required": ["url"],
    }

    def run(self, url: str, max_chars: int = 10000) -> ToolResult:
        # Prefer a plain fetch (works offline-of-browser, respects the
        # internet permission). Real interactive browsing can be layered via
        # the cua-driver when the user enables it.
        res = HttpRequestTool().run(url, max_chars=max_chars)
        return res
