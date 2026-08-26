"""Execution workspace.

Commands never run directly inside the user's project: each task gets its own
workspace directory, and stdout/stderr/return codes/durations are captured.

SCOPE OF THE ISOLATION — be precise about this, because the name invites
over-trust. What is enforced here is: a dedicated working directory, a wall
-clock timeout, an allowlisted environment (no credentials are inherited),
and an address-space ceiling via `resource` on POSIX. What is NOT enforced:
user, namespace, network or filesystem isolation, and on Windows not even
the memory ceiling. A process started here can still read the user's disk.
For untrusted code, run the whole application inside a container or VM.

This is the substrate the Terminal/Browser tools run on.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..core.security import SecurityEnforcer


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Convenience accessor (stdout + stderr) used by quality/engine code."""
        return (self.stdout or "") + (self.stderr or "")

    def __str__(self) -> str:
        head = f"exit={self.returncode} time={self.duration:.2f}s"
        if self.timed_out:
            head += " TIMEOUT"
        out = [head]
        if self.stdout.strip():
            out.append("--- stdout ---\n" + self.stdout.strip())
        if self.stderr.strip():
            out.append("--- stderr ---\n" + self.stderr.strip())
        return "\n".join(out)


class Sandbox:
    """Runs commands in an isolated workspace directory with timeout/mem caps."""

    def __init__(self, workspace: Path, security: Optional[SecurityEnforcer] = None,
                 timeout: int = 300, mem_mb: int = 4096, logger=None) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.security = security or SecurityEnforcer()
        self.timeout = timeout
        self.mem_mb = mem_mb
        self.logger = logger

    # Environment variables an agent-run subprocess is allowed to inherit.
    # Everything else (API keys, cloud credentials, SSH agent sockets, tokens)
    # is withheld: the code being executed was written moments ago by a model
    # acting on untrusted repository content.
    ENV_ALLOWLIST = (
        "PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
        "PROGRAMDATA", "SHELL", "USER", "LOGNAME",
    )

    def _build_env(self, env: Optional[dict] = None) -> dict:
        """Minimal allowlisted environment for a subprocess."""
        run_env = {k: v for k, v in os.environ.items() if k in self.ENV_ALLOWLIST}
        run_env["PYTHONUNBUFFERED"] = "1"
        # Never let a child re-enter the agent's own config or credentials.
        run_env.pop("YACINE_CONFIG", None)
        if env:
            run_env.update(env)
        return run_env

    def run(self, command, cwd: Optional[Path] = None, env: Optional[dict] = None,
            shell: Optional[bool] = None, timeout: Optional[int] = None) -> ExecResult:
        """Run a command in the workspace.

        ``command`` may be a string (run through the shell) or a list of
        arguments (run WITHOUT a shell). Callers that compose a command from
        model output must pass a list: string interpolation into a shell
        command is a command-injection vector.
        """
        is_list = isinstance(command, (list, tuple))
        if shell is None:
            shell = not is_list
        if is_list and shell:
            raise ValueError("Refusing to run an argv list through the shell.")
        display = " ".join(str(c) for c in command) if is_list else str(command)
        self.security.check_command(display)
        work = Path(cwd) if cwd else self.workspace
        # Confine the working directory to the workspace.
        work = self.security.check_path(work, for_write=False, root=self.workspace) \
            if Path(work) != Path(self.workspace) else Path(self.workspace)
        work.mkdir(parents=True, exist_ok=True)

        run_env = self._build_env(env)

        # Build the subprocess with a memory cap via a pre-exec hook (POSIX).
        preexec = None
        if os.name != "nt":
            try:
                import resource

                def _limit():
                    resource.setrlimit(resource.RLIMIT_AS, (self.mem_mb * 1024 * 1024, -1))
                preexec = _limit
            except Exception:
                preexec = None

        to = timeout if timeout is not None else self.timeout
        if self.logger:
            self.logger.tool("sandbox", f"$ {display[:200]} (cwd={work})")
        start = time.time()
        try:
            proc = subprocess.run(
                list(command) if is_list else command,
                shell=shell,
                cwd=str(work),
                env=run_env,
                capture_output=True,
                text=True,
                timeout=to,
                preexec_fn=preexec,
            )
            res = ExecResult(
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                duration=time.time() - start,
            )
        except subprocess.TimeoutExpired as e:
            res = ExecResult(
                returncode=-1,
                stdout=(e.stdout or b"").decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=f"Command timed out after {self.timeout}s",
                timed_out=True,
                duration=time.time() - start,
            )
        except Exception as e:  # noqa: BLE001
            res = ExecResult(returncode=-1, stdout="", stderr=str(e), duration=time.time() - start)

        if self.logger:
            self.logger.tool("sandbox", f"-> exit={res.returncode} t={res.duration:.2f}s")
        return res

    def resolve(self, rel: str) -> Path:
        """Resolve `rel` strictly inside the workspace (raises SecurityError)."""
        return self.security.resolve_in(self.workspace, rel)

    def write_file(self, rel: str, content: str) -> Path:
        """Write a file inside the sandbox (confined + security-gated)."""
        target = self.security.check_path(Path(rel), for_write=True, root=self.workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, rel: str) -> str:
        target = self.security.check_path(Path(rel), for_write=False, root=self.workspace)
        return target.read_text(encoding="utf-8", errors="ignore")

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).exists()
        except Exception:
            return False
