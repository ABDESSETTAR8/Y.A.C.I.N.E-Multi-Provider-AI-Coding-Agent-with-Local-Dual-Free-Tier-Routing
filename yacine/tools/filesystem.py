"""Filesystem tools: read, list, write, delete, rename, move, search-code.

All file operations are routed through the sandbox (security-gated) and
resolved against the project workspace, never the arbitrary host filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List

from .base import Tool, ToolResult
from .sandbox import Sandbox


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the contents of a file inside the project workspace. Returns the text."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file within the workspace."},
            "limit": {"type": "integer", "description": "Max lines to return (optional)."},
        },
        "required": ["path"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str, limit: int = 0) -> ToolResult:
        from ..core.security import SecurityEnforcer
        SecurityEnforcer().check_path(self.sandbox.workspace / path, for_write=False)
        p = self.sandbox.workspace / path
        if not p.exists():
            return ToolResult(ok=False, output=f"File not found: {path}")
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return ToolResult(ok=False, output=f"Cannot read {path}: {e}")
        if limit and limit > 0:
            text = "\n".join(text.splitlines()[:limit])
        return ToolResult(ok=True, output=text, data={"path": path, "bytes": len(text)})


class ListDirTool(Tool):
    name = "list_dir"
    description = "List files and directories inside the workspace (optionally a subpath)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory path, or '.' for workspace root."},
        },
        "required": [],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str = ".") -> ToolResult:
        base = self.sandbox.workspace / path
        if not base.exists():
            return ToolResult(ok=False, output=f"Path not found: {path}")
        entries = []
        for item in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name)):
            kind = "dir" if item.is_dir() else "file"
            entries.append(f"{kind:4} {item.name}")
        return ToolResult(ok=True, output="\n".join(entries) or "(empty)", data={"entries": entries})


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file in the workspace with the given content. Creates parent dirs."
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to write."},
            "content": {"type": "string", "description": "Full file content."},
        },
        "required": ["path", "content"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str, content: str) -> ToolResult:
        try:
            target = self.sandbox.write_file(path, content)
        except Exception as e:
            return ToolResult(ok=False, output=f"Write failed: {e}")
        return ToolResult(ok=True, output=f"Wrote {len(content)} chars to {path}",
                          data={"path": path})


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file or empty directory from the workspace."
    writes = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path to delete."}},
        "required": ["path"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str) -> ToolResult:
        from ..core.security import SecurityEnforcer
        SecurityEnforcer().check_delete(self.sandbox.workspace / path)
        target = self.sandbox.workspace / path
        if not target.exists():
            return ToolResult(ok=False, output=f"Not found: {path}")
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                return ToolResult(ok=False, output=f"Directory not empty: {path}")
        else:
            target.unlink()
        return ToolResult(ok=True, output=f"Deleted {path}")


class RenameMoveTool(Tool):
    name = "move_file"
    description = "Rename or move a file/directory within the workspace."
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string", "description": "Source relative path."},
            "dst": {"type": "string", "description": "Destination relative path."},
        },
        "required": ["src", "dst"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, src: str, dst: str) -> ToolResult:
        from ..core.security import SecurityEnforcer
        SecurityEnforcer().check_path(self.sandbox.workspace / dst, for_write=True)
        s = self.sandbox.workspace / src
        d = self.sandbox.workspace / dst
        if not s.exists():
            return ToolResult(ok=False, output=f"Source not found: {src}")
        d.parent.mkdir(parents=True, exist_ok=True)
        s.replace(d)
        return ToolResult(ok=True, output=f"Moved {src} -> {dst}")


class SearchCodeTool(Tool):
    name = "search_code"
    description = "Search file contents in the workspace for a regex pattern. Returns matching lines with file:line."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "Subdirectory to search (default '.')."},
            "glob": {"type": "string", "description": "Optional file glob filter (e.g. '*.py')."},
        },
        "required": ["pattern"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, pattern: str, path: str = ".", glob: str = "*") -> ToolResult:
        import re
        base = self.sandbox.workspace / path
        if not base.exists():
            return ToolResult(ok=False, output=f"Path not found: {path}")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return ToolResult(ok=False, output=f"Invalid regex: {e}")
        hits: List[str] = []
        files = base.rglob(glob) if glob else base.rglob("*")
        for f in files:
            if f.is_dir():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    rel = f.relative_to(self.sandbox.workspace)
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) >= 200:
                        break
        return ToolResult(ok=True, output="\n".join(hits) or "(no matches)",
                          data={"count": len(hits)})
