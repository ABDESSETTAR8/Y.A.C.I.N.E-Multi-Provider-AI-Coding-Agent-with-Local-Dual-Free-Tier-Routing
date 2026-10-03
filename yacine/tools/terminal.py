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

    def _pin_interpreter(self, cmd: str) -> str:
        """Rewrite a leading bare `python` to a real interpreter path.

        Models emit `python -m pytest -q` because that is what the training
        data says. On Linux and macOS `python` frequently does not exist, and
        where it does it resolves through PATH to some other interpreter that
        has none of the project's packages — so verification fails with "No
        module named pytest" while the code under test is fine, and the
        self-heal loop then spends its whole budget fixing nothing.
        """
        import re
        from .interpreter import python_executable

        py = python_executable(self.sandbox.workspace)
        if py == "python":
            return cmd
        # Use a lambda replacement to avoid re.sub interpreting backslashes
        # in the path (e.g. \U in C:\Users\... triggers a regex error).
        return re.sub(r'^\s*python(3(?:\.\d+)?)?(?=\s|$)',
                      lambda m: f'"{py}"', cmd, count=1)

    def run(self, command: str = "", cwd: str = "", timeout: int = 0,
            cmd: str = "", code: str = "", **kwargs) -> ToolResult:
        # Tolerate models that name the arg 'cmd' (or pass it positionally).
        cmd_text = command or cmd or code or kwargs.get("command") or kwargs.get("cmd") or ""
        if not cmd_text:
            return ToolResult(ok=False, output="terminal: no command provided",
                              data={"error": "missing command"})
        work = (self.sandbox.workspace / cwd) if cwd else None
        to = timeout or self.sandbox.timeout
        cmd_text = self._pin_interpreter(cmd_text)
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
        import shlex
        from .interpreter import python_executable
        from ..core.security import SecurityError

        py = python_executable(self.sandbox.workspace)
        try:
            extra = shlex.split(argv) if argv else []
        except ValueError as e:
            return ToolResult(ok=False, output=f"Could not parse argv: {e}")

        # If `code` names an existing .py file in the workspace, run it;
        # otherwise treat it as a snippet and run it from a scratch file.
        try:
            maybe = self.sandbox.resolve(code) if code.endswith(".py") else None
        except SecurityError as e:
            return ToolResult(ok=False, output=f"Refused: {e}")

        if maybe is not None and maybe.exists():
            target = str(maybe)
        else:
            try:
                target = str(self.sandbox.write_file("_yacine_run.py", code))
            except SecurityError as e:
                return ToolResult(ok=False, output=f"Refused: {e}")

        # An argv list, executed without a shell — `code` and `argv` both come
        # from the model and must never be interpolated into a command string.
        res = self.sandbox.run([py, target] + extra, shell=False)
        return ToolResult(ok=res.ok, output=str(res),
                          data={"returncode": res.returncode,
                                "timed_out": res.timed_out,
                                "interpreter": py})
