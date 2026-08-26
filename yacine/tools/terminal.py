"""Terminal tool: run shell/Python/Node/Docker commands inside the sandbox.

Captures stdout/stderr/return code/time. This is the "Execution" pillar of
the autonomous loop — tests, builds, installs all go through here.
"""
from __future__ import annotations

from .base import Tool, ToolResult
from .sandbox import Sandbox


class TerminalTool(Tool):
    name = "terminal"
    description = ("Run a shell command inside the isolated workspace. Use for executing "
                   "tests, builds, installs, git, python, node, docker, etc. "
                   "Dangerous commands are blocked by the security layer.")
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute."},
            "cwd": {"type": "string", "description": "Optional subdir to run in (relative to workspace)."},
            "timeout": {"type": "integer", "description": "Max seconds before timeout (default 300)."},
        },
        "required": ["command"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, command: str = "", cwd: str = "", timeout: int = 0,
            cmd: str = "", code: str = "", **kwargs) -> ToolResult:
        # Tolerate models that name the arg 'cmd' (or pass it positionally).
        cmd_text = command or cmd or code or kwargs.get("command") or kwargs.get("cmd") or ""
        if not cmd_text:
            return ToolResult(ok=False, output="terminal: no command provided",
                              data={"error": "missing command"})
        work = (self.sandbox.workspace / cwd) if cwd else None
        to = timeout or self.sandbox.timeout
        res = self.sandbox.run(cmd_text, cwd=work, timeout=to)
        return ToolResult(
            ok=res.ok,
            output=str(res),
            data={"returncode": res.returncode, "timed_out": res.timed_out,
                  "duration": res.duration},
        )


class PythonExecTool(Tool):
    name = "run_python"
    description = "Execute a Python snippet or file inside the workspace and capture its output."
    writes = True
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code or a path to a .py file to run."},
            "argv": {"type": "string", "description": "Optional arguments passed to the script."},
        },
        "required": ["code"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, code: str, argv: str = "") -> ToolResult:
        # If code looks like a path, run the file; else run inline.
        maybe = self.sandbox.workspace / code
        if maybe.exists() and code.endswith(".py"):
            cmd = f'"{self.sandbox.workspace}/venv/Scripts/python.exe" {code} {argv}'.replace("/", "\\") \
                if (self.sandbox.workspace / "venv").exists() else f'python {code} {argv}'
        else:
            script = self.sandbox.workspace / "_yacine_run.py"
            script.write_text(code, encoding="utf-8")
            cmd = f'python _yacine_run.py {argv}'
        res = self.sandbox.run(cmd)
        return ToolResult(ok=res.ok, output=str(res),
                          data={"returncode": res.returncode})
