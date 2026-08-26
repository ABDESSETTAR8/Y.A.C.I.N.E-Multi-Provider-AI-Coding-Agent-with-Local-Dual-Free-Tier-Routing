"""Code-edit tool: targeted find-and-replace edits (safe overwrite + backup).

This is how the Coder/Debugger agents modify existing code precisely without
rewriting whole files. Supports unique single replacement, all-occurrences,
and a fenced full-file rewrite.

Two properties this tool must have, and previously did not:

  * A single replacement is only applied when `old_string` matches EXACTLY
    ONCE. Editing the first of several matches silently changes the wrong line
    and reports success — the debugger then re-runs the same failing test and
    concludes its fix did not work.
  * Backups are versioned. A single `.bak` slot means the second edit to a
    file destroys the only copy of the original.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List

from .base import Tool, ToolResult
from .sandbox import Sandbox
from ..core.security import SecurityError

BACKUP_DIR = ".yacine_backups"


class _EditToolBase(Tool):
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def _resolve(self, path: str, for_write: bool = True) -> Path:
        return self.sandbox.security.check_path(
            Path(path), for_write=for_write, root=self.sandbox.workspace
        )

    def _backup_dir(self) -> Path:
        d = Path(self.sandbox.workspace) / BACKUP_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _backup(self, target: Path, text: str) -> Path:
        """Store a timestamped copy and return its path."""
        rel = target.relative_to(Path(self.sandbox.workspace).resolve())
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        safe = str(rel).replace("/", "__").replace("\\", "__")
        dest = self._backup_dir() / f"{safe}.{stamp}.bak"
        dest.write_text(text, encoding="utf-8")
        return dest

    def _backups_for(self, target: Path) -> List[Path]:
        rel = target.relative_to(Path(self.sandbox.workspace).resolve())
        safe = str(rel).replace("/", "__").replace("\\", "__")
        d = Path(self.sandbox.workspace) / BACKUP_DIR
        if not d.is_dir():
            return []
        return sorted(d.glob(f"{safe}.*.bak"))


class EditFileTool(_EditToolBase):
    name = "edit_file"
    description = ("Make a targeted edit to an existing file. Provide `path`, the exact "
                   "`old_string` to find, and `new_string` to replace it. `old_string` "
                   "must match exactly once unless replace_all=true — include "
                   "surrounding lines to make it unique. A timestamped backup is kept.")
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file."},
            "old_string": {"type": "string", "description": "Exact text to replace. Must be unique in the file."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(self, path: str, old_string: str, new_string: str,
            replace_all: bool = False) -> ToolResult:
        try:
            target = self._resolve(path, for_write=True)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        if not target.exists():
            return ToolResult(ok=False, output=f"File not found: {path}")
        if old_string == new_string:
            return ToolResult(ok=False, output="`old_string` and `new_string` are identical. No change made.")

        text = target.read_text(encoding="utf-8", errors="ignore")
        count = text.count(old_string)
        if count == 0:
            return ToolResult(ok=False,
                              output=f"`old_string` not found in {path}. No change made.")
        if count > 1 and not replace_all:
            lines = [str(i) for i, ln in enumerate(text.splitlines(), 1)
                     if old_string.splitlines()[0] in ln][:8]
            return ToolResult(
                ok=False,
                output=(f"`old_string` matches {count} times in {path} (near lines "
                        f"{', '.join(lines) or 'unknown'}). Refusing to guess which one "
                        f"you meant — add surrounding lines to make it unique, or set "
                        f"replace_all=true to change all {count}."),
                data={"matches": count})

        backup = self._backup(target, text)
        new_text = text.replace(old_string, new_string) if replace_all else \
            text.replace(old_string, new_string, 1)
        applied = count if replace_all else 1
        target.write_text(new_text, encoding="utf-8")
        return ToolResult(ok=True, output=f"Applied {applied} edit(s) to {path}",
                          data={"path": path, "count": applied,
                                "backup": backup.name})


class UndoEditTool(_EditToolBase):
    name = "undo_edit"
    description = "Restore the most recent backup for a file, undoing the last edit."
    writes = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Relative path whose backup to restore."}},
        "required": ["path"],
    }

    def run(self, path: str) -> ToolResult:
        try:
            target = self._resolve(path, for_write=True)
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")
        backups = self._backups_for(target)
        if not backups:
            return ToolResult(ok=False, output=f"No backup for {path}")
        latest = backups[-1]
        target.write_text(latest.read_text(encoding="utf-8", errors="ignore"),
                          encoding="utf-8")
        try:
            latest.unlink()   # consumed, so a second undo steps further back
        except OSError:
            pass
        remaining = len(backups) - 1
        return ToolResult(ok=True,
                          output=f"Restored {path} from {latest.name} "
                                 f"({remaining} earlier version(s) still available)",
                          data={"path": path, "remaining": remaining})
