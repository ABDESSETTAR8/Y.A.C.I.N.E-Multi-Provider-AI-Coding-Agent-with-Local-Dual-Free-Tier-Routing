"""Tests for the MCP client (Phase D1) using a fake stdio MCP server."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yacine.tools.mcp import connect_mcp
from yacine.tools.sandbox import Sandbox
from yacine.core.security import SecurityEnforcer


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "ws", security=SecurityEnforcer())


def test_mcp_connect_and_call(sandbox):
    server = [sys.executable, str(ROOT / "tests" / "fixtures" / "fake_mcp_server.py")]
    tools = connect_mcp(server, sandbox)
    assert tools, "expected at least one MCP tool from the fake server"
    echo = next((t for t in tools if t.name == "mcp_echo"), None)
    assert echo is not None, f"mcp_echo not found in {[t.name for t in tools]}"
    res = echo.run(msg="hello")
    assert res.ok and "hello" in res.output


def test_mcp_missing_server_returns_empty(sandbox):
    tools = connect_mcp(["this-binary-does-not-exist-xyz"], sandbox)
    assert tools == []
