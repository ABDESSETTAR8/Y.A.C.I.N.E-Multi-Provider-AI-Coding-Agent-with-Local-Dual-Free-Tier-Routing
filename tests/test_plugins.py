"""Plugin SDK tests: discovery, safe activation, and the sample hello_tool.

These run against the real ToolRegistry and the project's ``plugins/`` dir,
proving auto-discovery works without model access.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo importable regardless of cwd (matches tests/test_engine.py).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yacine.tools.registry import build_tool_registry  # noqa: E402
from yacine.tools.sandbox import Sandbox  # noqa: E402
from yacine.core.security import SecurityEnforcer  # noqa: E402
from yacine.plugins import load_all_plugins  # noqa: E402


@pytest.fixture
def registry(tmp_path):
    sandbox = Sandbox(tmp_path / "plugin_ws", security=SecurityEnforcer())
    return build_tool_registry(sandbox)


def test_hello_tool_plugin_discovery_and_activation(registry):
    plugins_dir = ROOT / "plugins"
    assert (plugins_dir / "hello_tool" / "plugin.yaml").is_file()

    loaded = load_all_plugins(registry, plugins_dir=plugins_dir)

    # The sample plugin must be discovered and activated.
    names = {p.name for p in loaded}
    assert "hello_tool" in names

    # Its tool must be registered into the live registry.
    assert "hello_tool" in registry.names()

    # Calling the tool returns an OK result containing the marker string.
    result = registry.call("hello_tool", {})
    assert result.ok is True
    assert "hello" in result.output


def test_broken_plugin_is_skipped_not_crashing(registry, tmp_path):
    """A plugin whose entrypoint raises on import must be skipped, not fatal."""
    broken = tmp_path / "broken_plugin"
    broken.mkdir()
    (broken / "plugin.yaml").write_text(
        "name: broken_plugin\nversion: '0.1.0'\n"
        "description: intentionally broken\nentrypoint: broken.py\n",
        encoding="utf-8",
    )
    (broken / "broken.py").write_text(
        "import this_module_does_not_exist_xyz\n", encoding="utf-8",
    )

    # Point discovery at the temp dir: should skip the broken one and return [].
    loaded = load_all_plugins(registry, plugins_dir=tmp_path)
    assert loaded == []
    # The engine registry is untouched and still functional.
    assert "hello_tool" not in registry.names()
