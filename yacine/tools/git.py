"""Git integration tool: init, status, add, commit, branch, diff, push (gated).

The DevOps/Agent uses this for commit, rollback, diff visualization, and
conflict-aware history. `git push` is gated behind the git_push permission
(default OFF) so nothing leaves the machine without explicit opt-in.
"""
from __future__ import annotations

from .base import Tool, ToolResult
from .sandbox import Sandbox
from ..core.security import SecurityEnforcer


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

    def run(self, operation: str, args: str = "") -> ToolResult:
        op = operation.lower()
        if op == "push":
            SecurityEnforcer().check_git_push()
        cmd_map = {
            "init": "git init",
            "status": "git status --porcelain",
            "diff": "git diff",
            "log": "git log --oneline -20",
        }
        if op in cmd_map:
            cmd = cmd_map[op]
        elif op == "add":
            cmd = f"git add {args or '.'}"
        elif op == "commit":
            msg = args or "chore: update via Y.A.C.I.N.E"
            cmd = f'git commit -m "{msg}"'
        elif op == "branch":
            cmd = f"git branch {args}"
        elif op == "checkout":
            cmd = f"git checkout {args}"
        elif op == "tag":
            cmd = f"git tag {args}"
        elif op == "push":
            cmd = f"git push {args}"
        else:
            return ToolResult(ok=False, output=f"Unsupported git operation: {operation}")
        res = self.sandbox.run(cmd)
        return ToolResult(ok=res.ok, output=str(res),
                          data={"operation": op, "returncode": res.returncode})
