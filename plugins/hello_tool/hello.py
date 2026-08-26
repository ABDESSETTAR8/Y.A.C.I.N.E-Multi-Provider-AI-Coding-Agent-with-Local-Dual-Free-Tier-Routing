"""Sample Y.A.C.I.N.E plugin: registers one trivial tool.

This exists to prove the Plugin SDK auto-discovery + activation path works
end-to-end. A real plugin would do something useful in ``run``.
"""
from __future__ import annotations

from yacine.tools.base import Tool, ToolResult


class HelloTool(Tool):
    name = "hello_tool"
    description = "Returns a friendly greeting emitted from a plugin."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    writes = False
    needs_internet = False

    def run(self, **kwargs: object) -> ToolResult:
        return ToolResult(ok=True, output="hello from plugin")


def register(registry) -> None:
    """Hook called by the Plugin SDK to attach this plugin's tools."""
    registry.register(HelloTool())
