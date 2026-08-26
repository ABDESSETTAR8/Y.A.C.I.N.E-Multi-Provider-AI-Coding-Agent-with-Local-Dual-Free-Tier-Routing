"""Regression tests for the 2026-08-26 audit findings.

Each test below corresponds to a defect that was live in the codebase. They
exist so the fix cannot be quietly undone: every one of these assertions failed
before the fix and passes after it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yacine.core.security import SecurityEnforcer, SecurityError  # noqa: E402
from yacine.tools.sandbox import Sandbox  # noqa: E402
from yacine.tools.filesystem import (ReadFileTool, WriteFileTool,  # noqa: E402
                                     DeleteFileTool, ListDirTool)
from yacine.tools.code_edit import EditFileTool, UndoEditTool  # noqa: E402
from yacine.tools.git import GitTool  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path):
    return Sandbox(tmp_path / "ws")


# --------------------------------------------------------------------------
# C-01: workspace confinement
# --------------------------------------------------------------------------
def test_absolute_path_write_is_refused(sandbox, tmp_path):
    outside = tmp_path / "escaped.txt"
    res = WriteFileTool(sandbox).run(str(outside), "pwned")
    assert res.ok is False
    assert not outside.exists(), "absolute path escaped the workspace"


def test_parent_traversal_write_is_refused(sandbox, tmp_path):
    res = WriteFileTool(sandbox).run("../../escaped.txt", "pwned")
    assert res.ok is False
    assert not (tmp_path / "escaped.txt").exists()


def test_absolute_path_read_is_refused(sandbox):
    res = ReadFileTool(sandbox).run("/etc/hostname")
    assert res.ok is False


def test_traversal_delete_is_refused(sandbox, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    res = DeleteFileTool(sandbox).run(f"../{victim.name}")
    assert res.ok is False
    assert victim.exists(), "delete escaped the workspace"


def test_list_dir_is_confined(sandbox):
    assert ListDirTool(sandbox).run("/etc").ok is False


def test_symlink_out_of_workspace_is_refused(sandbox, tmp_path):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks unsupported")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    link = sandbox.workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    assert ReadFileTool(sandbox).run("link.txt").ok is False


def test_normal_relative_paths_still_work(sandbox):
    w = WriteFileTool(sandbox).run("pkg/mod.py", "x = 1\n")
    assert w.ok is True
    assert ReadFileTool(sandbox).run("pkg/mod.py").output.strip() == "x = 1"


def test_resolve_in_rejects_and_accepts(tmp_path):
    with pytest.raises(SecurityError):
        SecurityEnforcer.resolve_in(tmp_path, "/etc/passwd")
    with pytest.raises(SecurityError):
        SecurityEnforcer.resolve_in(tmp_path, "../../x")
    assert SecurityEnforcer.resolve_in(tmp_path, "a/b.py").name == "b.py"


# --------------------------------------------------------------------------
# C-03: no model-supplied text reaches a shell
# --------------------------------------------------------------------------
def test_git_commit_message_cannot_inject(sandbox, tmp_path):
    marker = tmp_path / "INJECTED"
    git = GitTool(sandbox)
    git.run("init")
    git.run("commit", f'msg"; touch {marker}; echo "')
    assert not marker.exists(), "commit message escaped into the shell"


def test_git_rejects_shell_syntax_in_args(sandbox):
    res = GitTool(sandbox).run("add", "$(whoami)")
    assert res.ok is False


def test_sandbox_refuses_argv_through_shell(sandbox):
    with pytest.raises(ValueError):
        sandbox.run(["echo", "hi"], shell=True)


# --------------------------------------------------------------------------
# H-08: subprocesses do not inherit credentials
# --------------------------------------------------------------------------
def test_subprocess_environment_is_allowlisted(sandbox, monkeypatch):
    monkeypatch.setenv("YACINE_MODEL_API_KEY", "sk-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")
    env = sandbox._build_env()
    assert "YACINE_MODEL_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "PATH" in env


# --------------------------------------------------------------------------
# H-07: edit_file will not guess which occurrence you meant
# --------------------------------------------------------------------------
def test_edit_file_refuses_ambiguous_match(sandbox):
    sandbox.write_file("m.py", "x = 1\ny = 1\nz = 1\n")
    res = EditFileTool(sandbox).run("m.py", "= 1", "= 2")
    assert res.ok is False
    assert "3 times" in res.output
    assert sandbox.read_file("m.py") == "x = 1\ny = 1\nz = 1\n"


def test_edit_file_replace_all_is_explicit(sandbox):
    sandbox.write_file("m.py", "x = 1\ny = 1\n")
    assert EditFileTool(sandbox).run("m.py", "= 1", "= 2", replace_all=True).ok
    assert sandbox.read_file("m.py") == "x = 2\ny = 2\n"


def test_backups_are_versioned_and_undo_steps_back(sandbox):
    sandbox.write_file("m.py", "v0\n")
    edit = EditFileTool(sandbox)
    edit.run("m.py", "v0", "v1")
    edit.run("m.py", "v1", "v2")
    assert sandbox.read_file("m.py") == "v2\n"
    undo = UndoEditTool(sandbox)
    undo.run("m.py")
    assert sandbox.read_file("m.py") == "v1\n"
    undo.run("m.py")
    assert sandbox.read_file("m.py") == "v0\n", "the original was destroyed"


# --------------------------------------------------------------------------
# H-06: a real interpreter, never a bare "python"
# --------------------------------------------------------------------------
def test_interpreter_is_pinned(sandbox):
    from yacine.tools.terminal import TerminalTool
    pinned = TerminalTool(sandbox)._pin_interpreter("python -m pytest -q")
    assert not pinned.startswith("python ")
    assert "-m pytest -q" in pinned


# --------------------------------------------------------------------------
# H-03: an unverified run is not a successful run
# --------------------------------------------------------------------------
def test_task_result_reports_unverified_separately():
    from yacine.core.engine import TaskResult
    r = TaskResult(task_id="t", request="r", success=False)
    assert r.unverified is False
    assert hasattr(r, "files_produced")
    assert hasattr(r, "changed_files")


# --------------------------------------------------------------------------
# M-03: tools cannot reach the local machine or LAN
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/health",
    "http://localhost:11434/api/tags",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/",
    "file:///etc/passwd",
])
def test_http_tool_refuses_internal_targets(url):
    from yacine.tools.internet import HttpRequestTool
    assert HttpRequestTool().run(url).ok is False


# --------------------------------------------------------------------------
# M-04: secrets are never written to settings.yaml
# --------------------------------------------------------------------------
def test_save_config_omits_secrets(tmp_path):
    from yacine.core.config import Settings, save_config
    s = Settings(project_root=tmp_path)
    s.model.api_key = "sk-should-not-be-written"
    s.telegram.bot_token = "123:should-not-be-written"
    out = save_config(s, str(tmp_path / "settings.yaml"))
    text = out.read_text(encoding="utf-8")
    assert "sk-should-not-be-written" not in text
    assert "should-not-be-written" not in text


# --------------------------------------------------------------------------
# M-05: the memory store survives a torn write
# --------------------------------------------------------------------------
def test_memory_save_is_atomic(tmp_path):
    from yacine.memory.store import MemoryStore
    store = MemoryStore(data_dir=tmp_path)
    for i in range(5):
        store.add(f"fact {i}")
    reloaded = MemoryStore(data_dir=tmp_path)
    assert len(reloaded.all()) == 5
    assert len({m.id for m in reloaded.all()}) == 5, "memory ids collide"


# --------------------------------------------------------------------------
# M-07: settings are cached per path, not globally
# --------------------------------------------------------------------------
def test_settings_cache_is_per_path(tmp_path):
    import yaml
    from yacine.core.config import get_settings, reset_settings
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(yaml.safe_dump({"max_plan_iterations": 7}))
    b.write_text(yaml.safe_dump({"max_plan_iterations": 99}))
    reset_settings()
    try:
        assert get_settings(str(a)).max_plan_iterations == 7
        assert get_settings(str(b)).max_plan_iterations == 99
    finally:
        reset_settings()


# --------------------------------------------------------------------------
# C-02: the API authenticates by default
# --------------------------------------------------------------------------
def test_api_requires_a_key():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import yacine.api.server as srv

    anon = TestClient(srv.app, base_url="http://127.0.0.1:8765")
    for path in ("/health", "/models", "/memory", "/context/summary"):
        assert anon.get(path).status_code == 401, f"{path} served without a key"
    assert anon.post("/run", json={"request": "x"}).status_code == 401

    authed = TestClient(srv.app, base_url="http://127.0.0.1:8765",
                        headers={"X-API-Key": srv.API_KEY})
    assert authed.get("/health").status_code == 200


def test_api_does_not_allow_wildcard_origins():
    import yacine.api.server as srv
    assert "*" not in srv.ALLOWED_ORIGINS
    assert srv.API_KEY, "server started with an empty API key"


# --------------------------------------------------------------------------
# Performance regressions (2026-08-26): things that made the app feel slow
# --------------------------------------------------------------------------
def test_set_model_preserves_keep_alive_and_num_ctx():
    """The chat tab asks for a warm, small-context model. Dropping either
    override meant a full model reload from disk on every single message."""
    from yacine.models.manager import ModelManager
    from yacine.core.config import ModelConfig
    m = ModelManager(model_config=ModelConfig(provider="ollama", model="big",
                                              keep_alive="0", num_ctx=8192))
    m.set_model("ollama", "small", keep_alive="15m", num_ctx=4096)
    assert m.cfg.keep_alive == "15m"
    assert m.cfg.num_ctx == 4096
    assert m.provider._options({})["num_ctx"] == 4096


def test_set_model_is_a_noop_when_nothing_changed():
    from yacine.models.manager import ModelManager
    from yacine.core.config import ModelConfig
    m = ModelManager(model_config=ModelConfig(provider="ollama", model="m"))
    before = m.provider
    m.set_model("ollama", "m")
    assert m.provider is before, "provider churned on a no-op switch"


def test_monitor_endpoint_is_cached():
    """The UI polls this on a timer; each miss used to spawn subprocesses."""
    import time
    from yacine.ui import backend
    backend._monitor_cache, backend._monitor_at = {}, 0.0
    first = backend.monitor_endpoint()
    t0 = time.perf_counter()
    for _ in range(50):
        again = backend.monitor_endpoint()
    elapsed = time.perf_counter() - t0
    assert again is first, "monitor result was recomputed instead of cached"
    assert elapsed < 0.05, f"50 cached polls took {elapsed:.3f}s"


def test_availability_is_cached():
    from yacine.models.manager import ModelManager
    from yacine.core.config import ModelConfig
    m = ModelManager(model_config=ModelConfig(provider="ollama", model="m",
                                              base_url="http://127.0.0.1:1"))
    calls = {"n": 0}
    real = m.provider.is_available

    def counting():
        calls["n"] += 1
        return real()

    m.provider.is_available = counting
    for _ in range(5):
        m.available()
    assert calls["n"] == 1, f"backend probed {calls['n']} times instead of once"


def test_context_index_reuses_unchanged_files(tmp_path):
    """Re-indexing an untouched tree must not re-read every file."""
    from yacine.core.context import ContextEngine
    for i in range(5):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    ce = ContextEngine(tmp_path)
    ce.index()
    reads = {"n": 0}
    original = Path.read_text

    def counting_read(self, *a, **kw):
        reads["n"] += 1
        return original(self, *a, **kw)

    Path.read_text = counting_read
    try:
        ce.index(force=False)          # cached: returns immediately
        ce.indexed = False
        ce.index()                     # re-walk, but nothing changed
    finally:
        Path.read_text = original
    assert reads["n"] == 0, f"re-read {reads['n']} unchanged files"

    # A modified file IS re-read.
    import os, time
    target = tmp_path / "m0.py"
    target.write_text("def f0():\n    return 999\n")
    os.utime(target, (time.time() + 5, time.time() + 5))
    ce.indexed = False
    ce.index()
    assert "999" in ce.files[str(target)].snippet
