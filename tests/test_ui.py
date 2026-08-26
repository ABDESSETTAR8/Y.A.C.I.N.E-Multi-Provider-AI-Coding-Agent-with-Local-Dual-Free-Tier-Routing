"""Smoke test for the Desktop GUI backend (Phase E)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
except ImportError:
    pytest.skip("fastapi.testclient unavailable", allow_module_level=True)

from yacine.api.server import app
import yacine.api.server as srv

srv._settings.workspace_dir = ROOT / "tests" / "tmp_ui_ws"
srv._settings.workspace_dir.mkdir(parents=True, exist_ok=True)
# The API authenticates every non-public route and only serves allowed Hosts,
# so the test client must present the key and a permitted Host.
client = TestClient(app, base_url="http://127.0.0.1:8765",
                    headers={"X-API-Key": srv.API_KEY})


def test_ui_index_served():
    r = client.get("/ui")
    assert r.status_code == 200
    assert "Y.A.C.I.N.E" in r.text


def test_ui_static_assets():
    for asset in ("/ui/static/app.js", "/ui/static/styles.css"):
        r = client.get(asset)
        assert r.status_code == 200


def test_monitor_endpoint():
    r = client.get("/api/monitor")
    assert r.status_code == 200
    body = r.json()
    assert "system" in body  # may be error dict if psutil absent, but key present
    assert "timestamp" in body
