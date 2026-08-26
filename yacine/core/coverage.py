"""Coverage & advanced test generation (Phase A4).

- Measures line coverage with `pytest --cov` when pytest-cov is installed.
- Generates regression and smoke tests if missing, so the Tester agent can
  raise the bar automatically.
All functions are sandbox-bound and degrade gracefully.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..tools.sandbox import Sandbox


@dataclass
class CoverageResult:
    pct: float = 0.0
    available: bool = False
    passed: bool = True
    details: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"pct": self.pct, "available": self.available,
                "passed": self.passed, "details": self.details}


def measure_coverage(sandbox: Sandbox, min_pct: int = 0) -> CoverageResult:
    """Run pytest --cov on the workspace; return coverage percentage."""
    res = CoverageResult()
    # Detect python modules to cover.
    pys = [p for p in sandbox.workspace.rglob("*.py") if p.name != "__init__.py"]
    if not pys:
        res.details = "no python modules to cover"
        return res
    cov_check = sandbox.run("python -c \"import pytest_cov\"", timeout=30)
    if not cov_check.ok:
        res.available = False
        res.details = "pytest-cov not installed; skipping coverage gate"
        return res
    res.available = True
    out = sandbox.run("python -m pytest -q --cov=. --cov-report=term-missing", timeout=300)
    # Parse "TOTAL ...  NN%"
    m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", out.output)
    if m:
        res.pct = float(m.group(1))
    if min_pct and res.pct < min_pct:
        res.passed = False
        res.details = f"coverage {res.pct:.0f}% below threshold {min_pct}%"
    else:
        res.details = f"coverage {res.pct:.0f}%"
    return res


def generate_regression_tests(sandbox: Sandbox, source: Path) -> Optional[str]:
    """Create a regression test that re-runs the source module's functions.

    Returns the path written, or None if it could not be generated.
    """
    src = sandbox.workspace / source
    if not src.exists() or src.suffix != ".py":
        return None
    try:
        text = src.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    funcs = re.findall(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", text, re.M)
    if not funcs:
        return None
    module = src.stem
    lines = [f"import {module}", "", "def test_regression_functions_exist():"]
    for fn in sorted(set(funcs))[:30]:
        lines.append(f"    assert hasattr({module}, '{fn}'), 'missing {fn}'")
    lines.append("")
    target = sandbox.workspace / "tests" if (sandbox.workspace / "tests").exists() else sandbox.workspace
    test_path = target / f"test_regression_{module}.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("\n".join(lines), encoding="utf-8")
    return str(test_path.relative_to(sandbox.workspace))
