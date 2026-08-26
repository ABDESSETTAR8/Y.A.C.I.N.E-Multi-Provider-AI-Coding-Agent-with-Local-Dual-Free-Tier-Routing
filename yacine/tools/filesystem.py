"""Filesystem tools: read, list, write, delete, rename, move, search-code.

Every path argument is resolved through ``SecurityEnforcer.resolve_in()``
against the sandbox workspace, so absolute paths, ``..`` traversal and
symlinks pointing outside the workspace are rejected before any I/O happens.
Tools use the enforcer that was injected into the sandbox (which carries the
active permission set) rather than constructing a fresh one.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from .base import Tool, ToolResult
from .sandbox import Sandbox
from ..core.security import SecurityError


class _WorkspaceTool(Tool):
    """Base for tools bound to a sandbox workspace."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    @property
    def security(self):
        return self.sandbox.security

    def _resolve(self, path: str, for_write: bool = False) -> Path:
        """Resolve `path` inside the workspace or raise SecurityError."""
        return self.security.check_path(
            Path(path), for_write=for_write, root=self.sandbox.workspace
        )


class ReadFileTool(_WorkspaceTool):
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

    def run(self, path: str, limit: int = 0) -> ToolResult:
        try:
            p = self._resolve(path, for_write=False)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if not p.exists():
            return ToolResult(ok=False, output=f"File not found: {path}")
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return ToolResult(ok=False, output=f"Cannot read {path}: {e}")
        if limit and limit > 0:
            text = "\n".join(text.splitlines()[:limit])
        return ToolResult(ok=True, output=text, data={"path": path, "bytes": len(text)})


class ListDirTool(_WorkspaceTool):
    name = "list_dir"
    description = "List files and directories inside the workspace (optionally a subpath)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory path, or '.' for workspace root."},
        },
        "required": [],
    }

    def run(self, path: str = ".") -> ToolResult:
        try:
            base = self._resolve(path, for_write=False)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if not base.exists():
            return ToolResult(ok=False, output=f"Path not found: {path}")
        if not base.is_dir():
            return ToolResult(ok=False, output=f"Not a directory: {path}")
        entries = []
        for item in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name)):
            kind = "dir" if item.is_dir() else "file"
            entries.append(f"{kind:4} {item.name}")
        return ToolResult(ok=True, output="\n".join(entries) or "(empty)", data={"entries": entries})


class WriteFileTool(_WorkspaceTool):
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

    def run(self, path: str, content: str) -> ToolResult:
        try:
            target = self.sandbox.write_file(path, content)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        except Exception as e:
            return ToolResult(ok=False, output=f"Write failed: {e}")
        return ToolResult(ok=True, output=f"Wrote {len(content)} chars to {path}",
                          data={"path": str(target)})


class DeleteFileTool(_WorkspaceTool):
    name = "delete_file"
    description = "Delete a file or empty directory from the workspace."
    writes = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path to delete."}},
        "required": ["path"],
    }

    def run(self, path: str) -> ToolResult:
        try:
            target = self.security.check_delete(Path(path), root=self.sandbox.workspace)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if target == Path(self.sandbox.workspace).resolve():
            return ToolResult(ok=False, output="Refusing to delete the workspace root.")
        if not target.exists():
            return ToolResult(ok=False, output=f"Not found: {path}")
        try:
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
        except OSError as e:
            return ToolResult(ok=False, output=f"Delete failed for {path}: {e}")
        return ToolResult(ok=True, output=f"Deleted {path}")


class RenameMoveTool(_WorkspaceTool):
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

    def run(self, src: str, dst: str) -> ToolResult:
        try:
            s = self._resolve(src, for_write=False)
            d = self._resolve(dst, for_write=True)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if not s.exists():
            return ToolResult(ok=False, output=f"Source not found: {src}")
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            s.replace(d)
        except OSError as e:
            return ToolResult(ok=False, output=f"Move failed: {e}")
        return ToolResult(ok=True, output=f"Moved {src} -> {dst}")


class SearchCodeTool(_WorkspaceTool):
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

    MAX_HITS = 200

    def run(self, pattern: str, path: str = ".", glob: str = "*") -> ToolResult:
        import re
        try:
            base = self._resolve(path, for_write=False)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if not base.exists():
            return ToolResult(ok=False, output=f"Path not found: {path}")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return ToolResult(ok=False, output=f"Invalid regex: {e}")

        root = Path(self.sandbox.workspace).resolve()
        hits: List[str] = []
        truncated = False
        for f in base.rglob(glob or "*"):
            if len(hits) >= self.MAX_HITS:
                truncated = True
                break
            if f.is_dir():
                continue
            # Never follow a symlink out of the workspace.
            try:
                if root not in f.resolve().parents:
                    continue
            except OSError:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    rel = f.relative_to(root)
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) >= self.MAX_HITS:
                        truncated = True
                        break
        out = "\n".join(hits) or "(no matches)"
        if truncated:
            out += f"\n... (stopped at {self.MAX_HITS} matches; narrow the pattern or path)"
        return ToolResult(ok=True, output=out,
                          data={"count": len(hits), "truncated": truncated})
