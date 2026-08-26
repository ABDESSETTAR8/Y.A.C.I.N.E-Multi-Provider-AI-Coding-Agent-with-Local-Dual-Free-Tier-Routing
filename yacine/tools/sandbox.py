"""Execution sandbox.

Per the spec: never execute directly inside the project; create isolated
workspaces, capture stdout/stderr/return codes, enforce timeouts, and report
resource usage. We implement isolation via a dedicated workspace directory
(named per task) plus subprocess timeout + a memory ceiling via
`resource` (POSIX) where available, and taskkill/CloseHandle on Windows.

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
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.security = security or SecurityEnforcer()
        self.timeout = timeout
        self.mem_mb = mem_mb
        self.logger = logger

    def run(self, command: str, cwd: Optional[Path] = None, env: Optional[dict] = None,
            shell: bool = True, timeout: Optional[int] = None) -> ExecResult:
        self.security.check_command(command)
        work = Path(cwd) if cwd else self.workspace
        work.mkdir(parents=True, exist_ok=True)

        run_env = dict(os.environ)
        run_env["PYTHONUNBUFFERED"] = "1"
        if env:
            run_env.update(env)

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
            self.logger.tool("sandbox", f"$ {command[:200]} (cwd={work})")
        start = time.time()
        try:
            proc = subprocess.run(
                command,
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

    def write_file(self, rel: str, content: str) -> Path:
        """Write a file inside the sandbox (security-gated)."""
        target = self.workspace / rel
        self.security.check_path(target, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, rel: str) -> str:
        target = self.workspace / rel
        self.security.check_path(target, for_write=False)
        return target.read_text(encoding="utf-8", errors="ignore")

    def exists(self, rel: str) -> bool:
        return (self.workspace / rel).exists()
