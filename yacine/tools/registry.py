"""Tool registry factory: assembles the full default toolset bound to a sandbox."""
from __future__ import annotations

import logging

from ..core.config import get_settings
from ..core.security import SecurityEnforcer
from .base import ToolRegistry
from .sandbox import Sandbox
from .filesystem import (ReadFileTool, ListDirTool, WriteFileTool,
                         DeleteFileTool, RenameMoveTool, SearchCodeTool)
from .code_edit import EditFileTool, UndoEditTool
from .terminal import TerminalTool, PythonExecTool
from .internet import HttpRequestTool, WebSearchTool
from .browser import BrowserTool
from .git import GitTool
from .db import DBQueryTool, DBSchemaTool, DBMigrateTool
from .vision import VisionTool
from .voice import TextToSpeechTool, SpeechToTextTool


def build_tool_registry(workspace: Sandbox) -> ToolRegistry:
    """Create a registry with every built-in tool bound to `workspace`."""
    reg = ToolRegistry()
    reg.register(ReadFileTool(workspace))
    reg.register(ListDirTool(workspace))
    reg.register(WriteFileTool(workspace))
    reg.register(DeleteFileTool(workspace))
    reg.register(RenameMoveTool(workspace))
    reg.register(SearchCodeTool(workspace))
    reg.register(EditFileTool(workspace))
    reg.register(UndoEditTool(workspace))
    reg.register(TerminalTool(workspace))
    reg.register(PythonExecTool(workspace))
    reg.register(HttpRequestTool())
    reg.register(WebSearchTool())
    reg.register(BrowserTool())
    reg.register(GitTool(workspace))
    reg.register(DBQueryTool(workspace))
    reg.register(DBSchemaTool(workspace))
    reg.register(DBMigrateTool(workspace))
    reg.register(VisionTool(workspace))
    reg.register(TextToSpeechTool(workspace))
    reg.register(SpeechToTextTool(workspace))
    # Load MCP servers (graceful: failures are skipped, never crash) — but
    # never silently: a swallowed import or spawn error here means the agent
    # is missing tools with nothing anywhere to say why.
    try:
        from .mcp import load_mcp_from_config
        from ..core.config import get_settings as _gs
        load_mcp_from_config(reg, _gs(), workspace)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("yacine.tools").warning(
            "MCP servers not loaded: %s: %s", type(e).__name__, e)
    return reg


def make_sandbox(task_id: str, logger=None):
    settings = get_settings()
    ws = settings.workspace_dir / "tasks" / task_id
    sec = SecurityEnforcer(settings)
    return Sandbox(ws, security=sec, logger=logger)
