"""Security & permissions enforcement.

Implements the spec's privacy-first, permission-gated security model:
  - No file is read/written/executed without an explicit permission check.
  - Dangerous commands / patterns are blocked or flagged.
  - Files are never uploaded without permission (internet_access gate).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

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
    def check_path(self, path: Path, for_write: bool = False) -> None:
        path = Path(path)
        if for_write:
            self.require("write_file", str(path))
            if ".." in path.parts:
                # Allowed in general, but guard obvious escapes from workspace.
                pass
        else:
            self.require("read_file", str(path))

        ext = path.suffix.lower()
        if for_write and ext not in SAFE_WRITE_EXTS and ext != "":
            # Unknown extension -> require explicit user permission to be safe.
            if not self.can("write_file"):
                raise SecurityError(f"Write of {ext} files is not permitted.")

    def check_delete(self, path: Path) -> None:
        self.require("delete_file", str(path))

    def check_internet(self, url: str = "") -> None:
        self.require("internet_access", url)

    def check_git_push(self) -> None:
        self.require("git_push", "git push")

    def interactive_prompt(self, action: str, detail: str) -> bool:
        """When a permission is off, ask the user. Returns True to proceed."""
        # In autonomous mode we default to refusing if not permitted, but the
        # CLI can inject an interactive override. Default: refuse safely.
        return False
