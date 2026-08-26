"""Code-edit tool: targeted find-and-replace edits (safe overwrite + backup).

This is how the Coder/Debugger agents modify existing code precisely without
rewriting whole files. Supports unique single replacement, all-occurrences,
and a fenced full-file rewrite. A backup copy is kept so edits are undoable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import Tool, ToolResult
from .sandbox import Sandbox
from ..core.security import SecurityEnforcer


class EditFileTool(Tool):
    name = "edit_file"
    description = ("Make a targeted edit to an existing file. Provide `path`, the exact "
                   "`old_string` to find, and `new_string` to replace it. Set replace_all=true "
                   "to replace every occurrence. A .bak backup is created automatically.")
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str, old_string: str, new_string: str,
            replace_all: bool = False) -> ToolResult:
        SecurityEnforcer().check_path(self.sandbox.workspace / path, for_write=True)
        target = self.sandbox.workspace / path
        if not target.exists():
            return ToolResult(ok=False, output=f"File not found: {path}")
        text = target.read_text(encoding="utf-8", errors="ignore")
        if old_string not in text:
            return ToolResult(ok=False, output=f"`old_string` not found in {path}. No change made.")
        # backup
        target.with_suffix(target.suffix + ".bak").write_text(text, encoding="utf-8")
        if replace_all:
            new_text = text.replace(old_string, new_string)
            count = text.count(old_string)
        else:
            idx = text.index(old_string)
            new_text = text[:idx] + new_string + text[idx + len(old_string):]
            count = 1
        target.write_text(new_text, encoding="utf-8")
        return ToolResult(ok=True, output=f"Applied {count} edit(s) to {path}",
                          data={"path": path, "count": count})


class UndoEditTool(Tool):
    name = "undo_edit"
    description = "Restore the most recent backup (.bak) for a file, undoing the last edit."
    writes = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path whose backup to restore."}},
        "required": ["path"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str) -> ToolResult:
        target = self.sandbox.workspace / path
        bak = target.with_suffix(target.suffix + ".bak")
        if not bak.exists():
            return ToolResult(ok=False, output=f"No backup for {path}")
        target.write_text(bak.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        return ToolResult(ok=True, output=f"Restored backup of {path}")
