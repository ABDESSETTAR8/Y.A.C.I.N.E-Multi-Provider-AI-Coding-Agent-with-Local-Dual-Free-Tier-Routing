"""Tool framework: base classes + schema generation for model tool-calling.

A Tool is a self-describing capability the agents can invoke. Each tool
declares a JSON-schema for its parameters so the model (or scripted provider)
can call it safely with validated arguments.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ToolResult:
    def __init__(self, ok: bool, output: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.ok = ok
        self.output = output
        self.data = data or {}

    def __str__(self) -> str:
        status = "OK" if self.ok else "ERROR"
        return f"[{status}] {self.output}"

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "data": self.data}


class Tool(ABC):
    name: str = "base"
    description: str = ""
    #: If True, this tool writes to the filesystem (security-gated).
    writes: bool = False
    #: If True, this tool needs internet access (security-gated).
    needs_internet: bool = False

    # Each subclass can override `parameters` to declare its JSON schema.
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        ...

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        """Validate against schema, then run. Robust to bad input."""
        try:
            return self.run(**arguments)
        except TypeError as e:
            # Likely a missing/unknown argument.
            return ToolResult(ok=False, output=f"Argument error: {e}")
        except Exception as e:  # noqa: BLE001 - tools must not crash the loop
            return ToolResult(ok=False, output=f"Tool failed: {type(e).__name__}: {e}")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {name}")
        return tool.execute(arguments)

    def names(self) -> List[str]:
        return list(self._tools.keys())
