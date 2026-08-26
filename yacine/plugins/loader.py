"""Plugin SDK for Y.A.C.I.N.E.

A plugin is a folder under ``plugins_dir`` that contains a ``plugin.yaml``
manifest and a Python entrypoint module. The entrypoint may expose any of:

  * ``register(registry)``      -> add Tool instances to the ToolRegistry
  * ``register_agents()``       -> return a list of agent persona dicts
  * ``register_providers()``    -> return a list of model-provider classes
  * ``manifest``                -> a dict (optional; the parsed plugin.yaml is
                                   used by default)

Everything here is defensive: a malformed or crashing plugin is logged and
skipped, never allowed to take down the host engine.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("yacine.plugins")
# Avoid "No handler found" noise when the host hasn't configured logging.
logger.addHandler(logging.NullHandler())


def _default_plugins_dir() -> Path:
    """Resolve the plugins dir from settings, falling back to a sane default."""
    try:
        from ..core.config import get_settings

        return Path(get_settings().plugins_dir)
    except Exception:  # noqa: BLE001 - never let config failure break discovery
        here = Path(__file__).resolve()
        # yacine/plugins/loader.py -> project root is three levels up.
        return here.parents[2] / "plugins"


@dataclass
class Plugin:
    """A discovered plugin, lazily loaded on demand."""

    path: Path
    manifest: Dict[str, Any]
    name: str
    version: str
    description: str
    entrypoint: str
    # Populated by load(); kept out of the constructor signature.
    module: Optional[Any] = field(default=None, init=False)
    error: Optional[str] = field(default=None, init=False)

    def load(self) -> bool:
        """Import the entrypoint module safely. Returns True on success.

        On any failure the error is recorded, a warning isMessage delivered,
        and ``False`` is returned — the host must never crash here.
        """
        self.module = None
        self.error = None

        entry = (self.path / self.entrypoint)
        if not entry.is_file():
            self.error = f"entrypoint {self.entrypoint!r} not found in {self.path}"
            logger.warning("Plugin %s: %s", self.name, self.error)
            return False

        # Unique module name so two plugins with the same file name never clash.
        safe_dir = self.path.name.replace(".", "_").replace("-", "_")
        module_name = f"yacine_plugin_{safe_dir}"

        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                str(entry),
                submodule_search_locations=[str(self.path)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot build import spec for {entry}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self.module = module
            return True
        except Exception as e:  # noqa: BLE001 - a bad plugin must not crash us
            self.error = f"import failed: {type(e).__name__}: {e}"
            logger.warning("Plugin %s: %s", self.name, self.error)
            # Drop any half-imported module to keep sys.modules clean.
            sys.modules.pop(module_name, None)
            self.module = None
            return False

    def activate(self, registry: Any, agents: Optional[List[Any]] = None,
                 providers: Optional[List[Any]] = None) -> None:
        """Call the plugin's register hooks against the live containers.

        Each hook is invoked in its own try/except so a single misbehaving
        hook cannot prevent the others (or the engine) from working.
        """
        if self.module is None:
            if self.error:
                logger.warning(
                    "Plugin %s: not activated (load failed: %s)",
                    self.name, self.error,
                )
            return

        register = getattr(self.module, "register", None)
        if callable(register):
            try:
                register(registry)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Plugin %s: register() failed: %s: %s",
                    self.name, type(e).__name__, e,
                )

        if agents is not None:
            fn = getattr(self.module, "register_agents", None)
            if callable(fn):
                try:
                    for persona in (fn() or []):
                        agents.append(persona)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Plugin %s: register_agents() failed: %s: %s",
                        self.name, type(e).__name__, e,
                    )

        if providers is not None:
            fn = getattr(self.module, "register_providers", None)
            if callable(fn):
                try:
                    for provider in (fn() or []):
                        providers.append(provider)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Plugin %s: register_providers() failed: %s: %s",
                        self.name, type(e).__name__, e,
                    )


def discover_plugins(plugins_dir: Any) -> List[Plugin]:
    """Return every valid plugin folder (one with a ``plugin.yaml``)."""
    plugins_dir = Path(plugins_dir)
    found: List[Plugin] = []
    if not plugins_dir.is_dir():
        logger.info("Plugins directory %s does not exist; no plugins loaded.",
                    plugins_dir)
        return found

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "plugin.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = yaml.safe_load(
                manifest_path.read_text(encoding="utf-8")
            ) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Plugin %s: failed to parse plugin.yaml: %s",
                           entry.name, e)
            continue

        name = str(manifest.get("name") or entry.name)
        plugin = Plugin(
            path=entry,
            manifest=manifest,
            name=name,
            version=str(manifest.get("version", "0.0.0")),
            description=str(manifest.get("description", "")),
            entrypoint=str(manifest.get("entrypoint", "plugin.py")),
        )
        found.append(plugin)
    return found


def load_all_plugins(registry: Any, plugins_dir: Any = None,
                     agents: Optional[List[Any]] = None,
                     providers: Optional[List[Any]] = None) -> List[Plugin]:
    """Discover + load + activate every valid plugin under ``plugins_dir``.

    Returns the list of successfully activated ``Plugin`` objects. Plugins that
    fail to import or activate are logged and skipped — this never raises.
    """
    if plugins_dir is None:
        plugins_dir = _default_plugins_dir()
    plugins_dir = Path(plugins_dir)
    if not plugins_dir.exists():
        logger.info("Plugins dir %s missing; nothing to load.", plugins_dir)
        return []

    if agents is None:
        agents = []
    if providers is None:
        providers = []

    loaded: List[Plugin] = []
    for plugin in discover_plugins(plugins_dir):
        try:
            if plugin.load():
                plugin.activate(registry, agents, providers)
                loaded.append(plugin)
                logger.info("Plugin %s v%s loaded.", plugin.name, plugin.version)
        except Exception as e:  # noqa: BLE001 - ultimate safety net
            logger.warning("Plugin %s: skipped after error: %s: %s",
                           plugin.name, type(e).__name__, e)
    return loaded
