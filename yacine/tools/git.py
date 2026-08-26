"""Git integration tool: init, status, add, commit, branch, diff, push (gated).

The DevOps/Agent uses this for commit, rollback, diff visualization, and
conflict-aware history. `git push` is gated behind the git_push permission
(default OFF) so nothing leaves the machine without explicit opt-in.

Commands are built as ARGUMENT LISTS and executed without a shell. `args`
arrives from the model, which reads untrusted repository content and web
pages; interpolating it into a shell string is a command-injection vector.
"""
from __future__ import annotations

import shlex
from typing import List

from .base import Tool, ToolResult
from .sandbox import Sandbox
from ..core.security import SecurityError

# Operations whose `args` are free-form tokens (paths, refnames, flags).
_TOKEN_OPS = {"add", "branch", "checkout", "tag", "push"}


class GitTool(Tool):
    name = "git"
    description = ("Run git operations inside the workspace: status, add, commit, branch, diff, "
                   "log, init, tag. `push` requires the git_push permission.")
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string",
                          "description": "One of: init, status, add, commit, branch, checkout, diff, log, tag, push."},
            "args": {"type": "string", "description": "Arguments for the operation, e.g. commit message or file paths."},
        },
        "required": ["operation"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    @staticmethod
    def _tokens(args: str) -> List[str]:
        """Split free-form args into tokens without shell evaluation.

        shlex handles quoting, but performs no expansion: no globbing, no
        command substitution, no variable interpolation. Anything that still
        looks like shell metacharacter abuse is rejected outright.
        """
        if not args:
            return []
        try:
            toks = shlex.split(args, posix=True)
        except ValueError as e:
            raise SecurityError(f"Could not parse git arguments: {e}")
        for t in toks:
            if any(ch in t for ch in ("`", "$(", "\n", "\r")):
                raise SecurityError(f"Refusing git argument with shell syntax: {t!r}")
        return toks

    def run(self, operation: str, args: str = "") -> ToolResult:
        op = (operation or "").strip().lower()
        try:
            if op == "push":
                self.sandbox.security.check_git_push()

            if op == "init":
                argv = ["git", "init"]
            elif op == "status":
                argv = ["git", "status", "--porcelain"]
            elif op == "diff":
                argv = ["git", "diff"]
            elif op == "log":
                argv = ["git", "log", "--oneline", "-20"]
            elif op == "commit":
                # The message is ONE argument, never part of a command string.
                msg = args or "chore: update via Y.A.C.I.N.E"
                # A fresh task workspace usually has no git identity, which made
                # every auto-commit fail with "Author identity unknown". Supply a
                # per-command identity without touching the user's global config.
                argv = ["git",
                        "-c", "user.name=Y.A.C.I.N.E",
                        "-c", "user.email=yacine@localhost",
                        "commit", "-m", msg]
            elif op in _TOKEN_OPS:
                toks = self._tokens(args)
                if op == "add" and not toks:
                    toks = ["."]
                argv = ["git", op] + toks
            else:
                return ToolResult(ok=False, output=f"Unsupported git operation: {operation}")
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")

        res = self.sandbox.run(argv, shell=False)
        return ToolResult(ok=res.ok, output=str(res),
                          data={"operation": op, "returncode": res.returncode})
