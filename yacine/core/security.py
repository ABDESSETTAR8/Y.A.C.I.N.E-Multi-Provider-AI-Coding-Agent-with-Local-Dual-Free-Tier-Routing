"""Security & permissions enforcement.

Implements the spec's privacy-first, permission-gated security model:
  - No file is read/written/executed without an explicit permission check.
  - Dangerous commands / patterns are blocked or flagged.
  - Files are never uploaded without permission (internet_access gate).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from .config import get_settings

# Patterns that should never be executed by an autonomous agent without the
# user taking explicit control (destructive / system-level / exfiltration).
DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+-rf\s+/", "Recursive force delete of filesystem root"),
    (r"\bformat\b", "Disk format command"),
    (r"\bshutdown\b|\bhalt\b|\breboot\b", "System power control"),
    (r":\(\)\s*\{.*\}\s*;:", "Fork bomb"),
    (r"\bdd\b\s+if=.*of=/dev/", "Raw disk write via dd"),
    (r"\bchmod\s+-R\s+777\s+/", "World-write everything at root"),
    (r"\bmkfs\b", "Filesystem creation"),
    (r">\s*/dev/sd", "Direct block-device write"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(bash|sh|powershell)", "Pipe remote script to shell"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(bash|sh|powershell)", "Pipe remote script to shell"),
]

SAFE_WRITE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".rb", ".kt", ".swift", ".dart", ".html",
    ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".cfg",
    ".ini", ".lock", ".sql", ".sh", ".bat", ".ps1", ".xml", ".env", ".gitignore",
    ".dockerfile", ".r", ".scala", ".ex", ".exs", ".lua", ".vue",
}


class SecurityError(Exception):
    """Raised when an action is blocked by the security layer."""


class SecurityEnforcer:
    """Gatekeeper for all sensitive operations."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    # -- permissions ------------------------------------------------------
    def require(self, permission: str, what: str = "") -> None:
        if not self.settings.permissions.check(permission):
            raise SecurityError(
                f"Permission '{permission}' is disabled. Refusing: {what}"
            )

    def can(self, permission: str) -> bool:
        return self.settings.permissions.check(permission)

    # -- command safety ---------------------------------------------------
    def check_command(self, command: str) -> None:
        self.require("execute_command", command)
        lowered = command.lower()
        for pattern, reason in DANGEROUS_PATTERNS:
            if re.search(pattern, lowered):
                raise SecurityError(
                    f"Blocked potentially destructive command ({reason}): {command!r}"
                )

    # -- filesystem safety ------------------------------------------------
    @staticmethod
    def resolve_in(root: Path, rel: str | Path) -> Path:
        """Resolve ``rel`` strictly inside ``root``.

        This is THE containment boundary for every file tool. ``root / rel``
        alone is not a boundary: pathlib DISCARDS ``root`` when ``rel`` is
        absolute (``workspace / "/etc/passwd"`` -> ``/etc/passwd``), and plain
        ``..`` segments walk straight out. Both are rejected here, as are
        symlinks that point outside the root and Windows drive/UNC prefixes.
        """
        root = Path(root).resolve()
        rel_p = Path(rel)
        if rel_p.is_absolute() or rel_p.drive or rel_p.root:
            raise SecurityError(
                f"Absolute paths are not allowed; give a path relative to the "
                f"workspace (got {str(rel)!r})."
            )
        candidate = (root / rel_p).resolve()
        if candidate != root and root not in candidate.parents:
            raise SecurityError(
                f"Path escapes the workspace: {str(rel)!r} resolves outside "
                f"{root}. Refusing."
            )
        return candidate

    def check_path(self, path: Path, for_write: bool = False,
                   root: Optional[Path] = None) -> Path:
        """Gate a filesystem access. Returns the resolved, confined path.

        When ``root`` is given the path is confined to it (the caller should
        pass the sandbox workspace). ``path`` may be relative to that root or
        an already-joined path; either way the result is verified to sit
        inside the root before any permission check succeeds.
        """
        path = Path(path)
        if root is not None:
            root_r = Path(root).resolve()
            try:
                rel = path.relative_to(root_r) if path.is_absolute() else path
            except ValueError:
                # An absolute path outside the root: let resolve_in reject it
                # with the clearer message.
                rel = path
            path = self.resolve_in(root_r, rel)
        else:
            path = path.resolve()

        if for_write:
            self.require("write_file", str(path))
        else:
            self.require("read_file", str(path))

        ext = path.suffix.lower()
        if for_write and ext not in SAFE_WRITE_EXTS and ext != "":
            # Unknown extension -> require explicit user permission to be safe.
            if not self.can("write_file"):
                raise SecurityError(f"Write of {ext} files is not permitted.")
        return path

    def check_delete(self, path: Path, root: Optional[Path] = None) -> Path:
        """Gate a delete. Returns the resolved, confined path."""
        path = Path(path)
        if root is not None:
            root_r = Path(root).resolve()
            try:
                rel = path.relative_to(root_r) if path.is_absolute() else path
            except ValueError:
                rel = path
            path = self.resolve_in(root_r, rel)
        else:
            path = path.resolve()
        self.require("delete_file", str(path))
        return path

    def check_internet(self, url: str = "") -> None:
        self.require("internet_access", url)

    def check_git_push(self) -> None:
        self.require("git_push", "git push")

    def interactive_prompt(self, action: str, detail: str) -> bool:
        """When a permission is off, ask the user. Returns True to proceed."""
        # In autonomous mode we default to refusing if not permitted, but the
        # CLI can inject an interactive override. Default: refuse safely.
        return False
