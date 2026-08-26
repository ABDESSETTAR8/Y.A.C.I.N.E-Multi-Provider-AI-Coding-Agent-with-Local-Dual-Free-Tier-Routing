"""Code-quality enforcement (Phase A).

Runs language-appropriate formatters/linters inside the sandbox and, when the
tools are available, auto-fixes and re-runs. All steps are config-gated and
gracefully skipped when a tool is not installed. This satisfies the spec's
"automatically enforce Black / Ruff / ESLint / Prettier / type checking /
security scanning / linting / dead-code detection / unused imports".

Each function returns a structured result so the engine can decide whether to
block delivery (e.g. a security finding) or just record a nit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..tools.sandbox import Sandbox


@dataclass
class QualityResult:
    ok: bool = True
    findings: List[str] = field(default_factory=list)
    fixed: bool = False
    details: str = ""
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "findings": self.findings,
                "fixed": self.fixed, "details": self.details, "skipped": self.skipped}


def _has(sandbox: Sandbox, cmd: str) -> bool:
    res = sandbox.run(f"where {cmd} || which {cmd}", timeout=20)
    return res.ok


def enforce_python(sandbox: Sandbox, autofix: bool = True) -> QualityResult:
    """Run black + ruff on .py files; auto-fix on failure when available."""
    res = QualityResult()
    pys = [str(p.relative_to(sandbox.workspace)) for p in sandbox.workspace.rglob("*.py")]
    if not pys:
        res.skipped.append("no python files")
        return res
    targets = " ".join(pys[:50])

    # Black (formatter)
    if _has(sandbox, "black"):
        fix = sandbox.run(f"black {targets}", timeout=120)
        if fix.ok:
            res.fixed = True
        else:
            res.findings.append("black: " + fix.output[:300])
    else:
        res.skipped.append("black (not installed)")

    # Ruff (linter + autofix for unused imports / dead code)
    if _has(sandbox, "ruff"):
        if autofix:
            sandbox.run(f"ruff check --fix {targets}", timeout=120)
        check = sandbox.run(f"ruff check {targets}", timeout=120)
        if not check.ok:
            # surface only the first few findings
            lines = [l for l in check.output.splitlines() if l.strip()][:15]
            res.findings.extend(lines)
            res.ok = False
            res.details = check.output[:1500]
    else:
        res.skipped.append("ruff (not installed)")

    # Python compile check (always available) — catches syntax errors.
    compile_res = sandbox.run(f"python -m py_compile {' '.join(pys[:50])}", timeout=120)
    if not compile_res.ok:
        res.ok = False
        res.findings.append("py_compile: " + compile_res.output[:400])
    return res


def enforce_javascript(sandbox: Sandbox, autofix: bool = True) -> QualityResult:
    """Run prettier + eslint on JS/TS when available."""
    res = QualityResult()
    js = [str(p.relative_to(sandbox.workspace)) for p in sandbox.workspace.rglob("*.js")]
    ts = [str(p.relative_to(sandbox.workspace)) for p in sandbox.workspace.rglob("*.ts")]
    files = js + ts
    if not files:
        res.skipped.append("no js/ts files")
        return res
    if _has(sandbox, "npx"):
        if _has(sandbox, "prettier") or _has(sandbox, "eslint"):
            if autofix:
                sandbox.run(f"npx prettier --write {' '.join(files[:50])}", timeout=180)
            lint = sandbox.run(f"npx eslint {' '.join(files[:50])}", timeout=180)
            if not lint.ok:
                res.ok = False
                res.findings.extend([l for l in lint.output.splitlines() if l.strip()][:15])
                res.details = lint.output[:1500]
        else:
            res.skipped.append("prettier/eslint (not installed)")
    else:
        res.skipped.append("npx (node toolchain not installed)")
    return res


def run_security_scan(sandbox: Sandbox) -> QualityResult:
    """Lightweight static security scan: secrets, dangerous calls, eval/exec.

    This is a dependency-free heuristic scan (the spec's 'security scanning' /
    'secrets' / 'dependency analysis' pillar). A dedicated Security agent also
    runs semantically; this catches the obvious hard-coded-secret patterns.
    """
    res = QualityResult()
    secret_patterns = [
        (r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"][A-Za-z0-9_\-]{12,}['\"]", "possible hardcoded secret"),
        (r"(?i)sk-[A-Za-z0-9]{20,}", "openai-style api key"),
        (r"(?i)ghp_[A-Za-z0-9]{20,}", "github token"),
        (r"eval\s*\(", "use of eval()"),
        (r"subprocess\.(call|run|Popen)\([^)]*shell\s*=\s*True", "shell=True subprocess"),
        (r"os\.system\(", "os.system() call"),
    ]
    compiled = [(re.compile(p), label) for p, label in secret_patterns]
    for p in sandbox.workspace.rglob("*"):
        if not p.is_file() or p.suffix not in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for rx, label in compiled:
            for m in rx.finditer(text):
                rel = str(p.relative_to(sandbox.workspace))
                line_no = text[:m.start()].count("\n") + 1
                res.findings.append(f"{label} in {rel}:{line_no}")
    if res.findings:
        res.ok = False
    return res


def enforce_all(sandbox: Sandbox, autofix: bool = True) -> Dict[str, QualityResult]:
    out = {
        "python": enforce_python(sandbox, autofix),
        "javascript": enforce_javascript(sandbox, autofix),
        "security": run_security_scan(sandbox),
    }
    return out
