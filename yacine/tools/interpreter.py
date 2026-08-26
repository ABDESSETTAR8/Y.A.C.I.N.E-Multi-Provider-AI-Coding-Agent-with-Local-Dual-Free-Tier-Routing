"""Locating the Python interpreter a workspace should be run with.

Hardcoding the string ``python`` is wrong twice over: on Linux and macOS the
name frequently does not exist at all (only ``python3``), and even where it
does it resolves through PATH to whatever interpreter happens to be first —
not the one running Y.A.C.I.N.E, and not the workspace's virtualenv. The
result is a verification step that fails with "No module named pytest" while
the code under test is perfectly fine, which then burns the entire self-heal
budget fixing a bug that does not exist.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def venv_python(workspace: Path) -> Optional[Path]:
    """Return the interpreter of a virtualenv inside `workspace`, if any."""
    ws = Path(workspace)
    for venv_name in ("venv", ".venv", "env"):
        for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
            cand = ws / venv_name / rel
            if cand.exists():
                return cand
    return None


def python_executable(workspace: Optional[Path] = None) -> str:
    """The interpreter to run workspace code with.

    Prefers a virtualenv inside the workspace (that is where the agent will
    have installed the project's dependencies); otherwise the interpreter
    currently running, which is guaranteed to exist and to have the packages
    Y.A.C.I.N.E itself depends on.
    """
    if workspace is not None:
        found = venv_python(workspace)
        if found is not None:
            return str(found)
    return sys.executable or "python3"
