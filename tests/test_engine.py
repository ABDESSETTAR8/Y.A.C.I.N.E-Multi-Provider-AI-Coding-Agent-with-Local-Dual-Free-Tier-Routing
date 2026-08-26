"""Tests for Y.A.C.I.N.E.

These tests run the REAL engine with the ScriptedProvider (no model needed),
proving that:
  1. Components import and the tool registry works.
  2. The execution sandbox captures stdout/stderr/return-codes correctly.
  3. The context engine indexes a repo and search returns hits.
  4. The memory store persists + similarity-searches.
  5. The FULL autonomous loop completes a verifiable task (self-healing from a
     deliberate bug to green) — this is the headline proof that Y.A.C.I.N.E
     behaves as an autonomous engineer, not a code generator.
  6. The REST API serves /health and /run (offline).
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path

import pytest

# Make the repo importable regardless of cwd.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yacine.core.config import ModelConfig, Permissions, get_settings
from yacine.models.manager import ModelManager
from yacine.models.scripted import ScriptedProvider
from yacine.tools.registry import build_tool_registry, make_sandbox
from yacine.tools.sandbox import Sandbox
from yacine.core.context import ContextEngine
from yacine.memory.store import MemoryStore, HashedEmbedder
from yacine.core.engine import AutonomousEngine, TaskResult

FIXTURE = ROOT / "tests" / "fixtures" / "scripted_task.yaml"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def scripted_manager(tmp_path):
    cfg = ModelConfig(provider="scripted", model="scripted", script_path=str(FIXTURE))
    return ModelManager(model_config=cfg)


@pytest.fixture
def sandbox(tmp_path):
    from yacine.core.security import SecurityEnforcer
    return Sandbox(tmp_path / "ws", security=SecurityEnforcer())


# --------------------------------------------------------------------------
# 1) Imports / registry
# --------------------------------------------------------------------------
def test_imports_and_registry(sandbox):
    reg = build_tool_registry(sandbox)
    names = set(reg.names())
    for expected in ("read_file", "write_file", "edit_file", "terminal",
                     "search_code", "git", "web_search"):
        assert expected in names, f"missing tool {expected}"
    assert len(reg.schemas()) == len(reg.all())


# --------------------------------------------------------------------------
# 2) Sandbox execution
# --------------------------------------------------------------------------
def test_sandbox_captures_success(sandbox):
    res = sandbox.run('python -c "print(21*2)"')
    assert res.ok
    assert "42" in res.stdout
    assert res.returncode == 0


def test_sandbox_captures_failure_and_timeout(sandbox):
    res = sandbox.run('python -c "raise SystemExit(3)"')
    assert not res.ok
    assert res.returncode == 3
    # timeout
    to = sandbox.run("python -c \"import time; time.sleep(5)\"", timeout=1)
    assert to.timed_out


def test_sandbox_blocks_dangerous(sandbox):
    from yacine.core.security import SecurityError
    with pytest.raises(SecurityError):
        sandbox.run("rm -rf /")


# --------------------------------------------------------------------------
# 3) Context engine
# --------------------------------------------------------------------------
def test_context_index_and_search(tmp_path):
    (tmp_path / "app.py").write_text("def calculate_tax(amount):\n    return amount * 0.2\n")
    ce = ContextEngine(tmp_path)
    n = ce.index()
    assert n >= 1
    hits = ce.search("calculate_tax")
    assert hits and "app.py" in hits[0].rel
    summary = ce.summary()
    assert "app.py" in summary


# --------------------------------------------------------------------------
# 4) Memory store
# --------------------------------------------------------------------------
def test_memory_add_search_persist(tmp_path):
    ms = MemoryStore(data_dir=tmp_path, embedder=HashedEmbedder())
    ms.add("User prefers pytest for testing", category="preference")
    ms.add("Project uses FastAPI", category="project")
    found = ms.search("testing framework preference")
    assert any("pytest" in m.content for m in found)
    # persistence: reload from disk
    ms2 = MemoryStore(data_dir=tmp_path, embedder=HashedEmbedder())
    assert len(ms2.all()) >= 2


# --------------------------------------------------------------------------
# 5) FULL AUTONOMOUS LOOP (self-healing) — the headline proof
# --------------------------------------------------------------------------
def test_autonomous_loop_completes_task_with_self_heal(scripted_manager, tmp_path):
    from yacine.core.config import get_settings, Settings
    settings = get_settings()
    # Point workspace at a temp dir for isolation.
    settings.workspace_dir = tmp_path / "workspace"
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)

    engine = AutonomousEngine(model_manager=scripted_manager, config_path=None)
    # Monkeypatch workspace_dir so tasks land under tmp.
    engine.settings.workspace_dir = tmp_path / "workspace"

    result = engine.run(
        request="Create a calc module with add and mul and unit tests",
        verify_command="python -m pytest -q",
        auto_commit=False,
    )
    # The buggy module must have been created and then fixed by the debugger.
    calc = None
    for ws in (tmp_path / "workspace" / "tasks").glob("*/calc.py"):
        calc = ws
    assert calc is not None, "calc.py was never created by the loop"
    fixed_src = calc.read_text()
    assert "a + b" in fixed_src, "self-heal did not fix the buggy add()"
    assert "a - b" not in fixed_src, "bug remained after self-heal"
    # Test file must exist and the loop must report success.
    assert (calc.parent / "test_calc.py").exists()
    assert result.success is True, f"loop reported failure: {result.verdict}\n{result.error}"
    assert result.self_heals >= 1, "expected at least one self-heal iteration"
    print("\n=== TASK RESULT ===")
    print(f"success={result.success} heals={result.self_heals} "
          f"tools={result.tool_calls} dur={result.duration:.2f}s")
    print("verdict:", result.verdict)


# --------------------------------------------------------------------------
# 6) REST API smoke (offline)
# --------------------------------------------------------------------------
def test_api_health_and_run(scripted_manager):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi.testclient unavailable")
    # Build an app instance with the scripted manager injected.
    from yacine.api.server import app as api_app
    # Inject scripted manager via env-free path: patch the module-level manager.
    import yacine.api.server as srv
    srv._manager = scripted_manager
    # Also point settings workspace to a temp dir.
    srv._settings.workspace_dir = ROOT / "tests" / "tmp_api_ws"
    srv._settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    client = TestClient(api_app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "model_provider" in body
    # Run a task via API.
    r2 = client.post("/run", json={"request": "make a calc module",
                                   "verify_command": "python -m pytest -q",
                                   "auto_commit": False})
    assert r2.status_code == 200
    assert r2.json()["success"] is True


# --------------------------------------------------------------------------
# 7) Phase A: code-quality + coverage + context upgrades
# --------------------------------------------------------------------------
def test_security_scan_detects_secret(tmp_path):
    from yacine.core.quality import run_security_scan
    from yacine.tools.sandbox import Sandbox
    from yacine.core.security import SecurityEnforcer
    ws = tmp_path / "ws"
    sb = Sandbox(ws, security=SecurityEnforcer())
    (ws / "secret.py").write_text('API_KEY = "sk-1234567890abcdefGHIJ"\n')
    res = run_security_scan(sb)
    assert res.ok is False
    assert any("secret" in f.lower() or "sk-" in f for f in res.findings)


def test_coverage_not_available_graceful(tmp_path):
    from yacine.core.coverage import measure_coverage
    from yacine.tools.sandbox import Sandbox
    from yacine.core.security import SecurityEnforcer
    ws = tmp_path / "ws"
    sb = Sandbox(ws, security=SecurityEnforcer())
    (ws / "m.py").write_text("def f():\n    return 1\n")
    res = measure_coverage(sb, min_pct=0)
    # pytest-cov likely not installed -> available False, still returns object.
    assert res.available is False


def test_context_git_history_and_style(tmp_path):
    from yacine.core.context import ContextEngine
    import subprocess
    (tmp_path / "app.py").write_text(
        "def main():\n    pass\n\n\ndef helper(x: int) -> int:\n    return x\n")
    try:
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init app"],
                       check=True, capture_output=True)
    except Exception:
        pass
    ce = ContextEngine(tmp_path)
    ce.index()
    hist = ce.git_history(limit=5)
    style = ce.coding_style()
    # Either we have a git repo (history present) or not; style must still detect python.
    assert style.get("language") == "python"
    if ce.is_git_repo():
        assert any("init app" in h for h in hist)


def test_research_gate_heuristics():
    from yacine.core.engine import AutonomousEngine
    eng = AutonomousEngine()
    assert eng._needs_research("How to use the Stripe API in Python") is True
    assert eng._needs_research("Build a FastAPI endpoint for items") is True
    assert eng._needs_research("Create a calc module with add and mul") is False


def test_run_research_skipped_when_not_needed():
    from yacine.core.engine import AutonomousEngine, TaskResult
    from yacine.models.manager import ModelManager
    from yacine.core.config import ModelConfig
    cfg = ModelConfig(provider="scripted", model="scripted",
                     script_path="tests/fixtures/scripted_task.yaml")
    eng = AutonomousEngine(model_manager=ModelManager(model_config=cfg))
    res = TaskResult(task_id="t", request="Create a calc module", success=False)
    # No research turn in fixture; gate should return "" for non-research request.
    out = eng._run_research("Create a calc module with add and mul", res)
    assert out == ""


# --------------------------------------------------------------------------
# 8) Phase C: Database tools (SQLite path, no external deps)
# --------------------------------------------------------------------------
def test_db_tools_sqlite(sandbox):
    from yacine.tools.db import DBQueryTool, DBSchemaTool, DBMigrateTool
    db = DBQueryTool(sandbox)
    # create + insert + select
    r1 = db.execute({"kind": "sqlite", "target": "demo.db",
                     "query": "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)"})
    assert r1.ok
    db.execute({"kind": "sqlite", "target": "demo.db",
                "query": "INSERT INTO t(v) VALUES ('hello')"})
    r2 = db.execute({"kind": "sqlite", "target": "demo.db",
                     "query": "SELECT * FROM t"})
    assert r2.ok and "hello" in r2.output
    # schema discovery
    sch = DBSchemaTool(sandbox).execute({"kind": "sqlite", "target": "demo.db"})
    assert sch.ok and "t" in sch.output
    # migration generation
    mig = DBMigrateTool(sandbox).execute({
        "name": "add_users",
        "schema": '{"users": {"id": "INTEGER PRIMARY KEY", "name": "TEXT"}}'})
    assert mig.ok and "CREATE TABLE" in mig.output and "DROP TABLE" in mig.output



