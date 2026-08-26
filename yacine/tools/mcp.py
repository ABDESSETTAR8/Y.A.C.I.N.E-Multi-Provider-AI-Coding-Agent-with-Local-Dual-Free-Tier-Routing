"""MCP client (Phase D1).

Connects to an MCP (Model Context Protocol) server and exposes its tools
inside Y.A.C.I.N.E's ToolRegistry. MCP uses JSON-RPC 2.0 over stdio
(stdin/stdout) or HTTP+SSE.

This implementation:
  - Spawns the server as a subprocess (stdio transport).
  - Performs `initialize` -> `tools/list` -> per-tool `tools/call`.
  - Wraps each discovered MCP tool as a `MCPTool` (a yacine Tool), so the
    agents can call MCP tools exactly like built-ins.

It is fully self-contained and degrades gracefully: if the server binary is
missing or the protocol errors, `connect_mcp` returns None and the engine
continues without MCP tools.
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Dict, List, Optional

from ..tools.base import Tool, ToolResult
from ..tools.sandbox import Sandbox


class MCPClient:
    """Minimal JSON-RPC 2.0 over stdio MCP client."""

    def __init__(self, command: List[str], timeout: float = 30.0,
                 logger=None) -> None:
        self.command = command
        self.timeout = timeout
        self.logger = logger
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._id = 0
        self.server_info: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []

    # -- transport -------------------------------------------------------
    def _send(self, method: str, params: Optional[dict] = None) -> dict:
        with self._lock:
            self._id += 1
            msg_id = self._id
            payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            # Read until we get a response with matching id (skip notifications).
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server closed connection")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == msg_id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP error: {msg['error']}")
                    return msg.get("result", {})

    def connect(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
        except FileNotFoundError:
            if self.logger:
                self.logger.warning("mcp", f"MCP server not found: {self.command}")
            return False
        # initialize
        result = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "yacine", "version": "0.1.0"},
        })
        self.server_info = result.get("serverInfo", {})
        # Notify initialized
        try:
            self.proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        listed = self._send("tools/list", {})
        self.tools = listed.get("tools", [])
        if self.logger:
            self.logger.info("mcp", f"Connected to {self.server_info} "
                                   f"with {len(self.tools)} tools")
        return True

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._send("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


class MCPTool(Tool):
    """A yacine Tool backed by an MCP server tool."""

    def __init__(self, client: MCPClient, spec: Dict[str, Any], sandbox: Sandbox) -> None:
        self.client = client
        self.spec = spec
        self.name = f"mcp_{spec['name']}"
        self.description = spec.get("description", f"MCP tool {spec['name']}")
        self.parameters = spec.get("inputSchema", {"type": "object", "properties": {}})
        self.sandbox = sandbox

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            res = self.client.call_tool(self.spec["name"], kwargs)
        except Exception as e:
            return ToolResult(ok=False, output=f"MCP call failed: {e}")
        # MCP tools/call returns {content: [...], isError: bool}
        content = res.get("content", [])
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        is_error = res.get("isError", False)
        return ToolResult(ok=not is_error, output=text, data=res)


def connect_mcp(command: List[str], sandbox: Sandbox, logger=None) -> List[MCPTool]:
    """Connect to an MCP server and return its tools as yacine Tools (or [])."""
    client = MCPClient(command, logger=logger)
    if not client.connect():
        return []
    tools = [MCPTool(client, spec, sandbox) for spec in client.tools]
    return tools


def load_mcp_from_config(registry, settings, sandbox, logger=None) -> int:
    """Load MCP servers declared under settings.model? No — read a dedicated
    mcp config section if present (in config/settings.yaml under `mcp_servers`).
    Returns the number of MCP tools registered."""
    mcp = getattr(settings, "mcp_servers", None) or {}
    count = 0
    for name, spec in mcp.items():
        cmd = spec.get("command")
        if not cmd:
            continue
        try:
            tools = connect_mcp(cmd, sandbox, logger=logger)
            for t in tools:
                registry.register(t)
                count += 1
        except Exception as e:
            if logger:
                logger.warning("mcp", f"Failed to load MCP server {name}: {e}")
    return count
