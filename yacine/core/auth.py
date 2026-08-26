"""The API key that guards the local server.

This server can run shell commands, so it authenticates every request. That
creates a chicken-and-egg problem for the bundled UI: the page is served from
the same process, but it still has to present a key on every call.

Resolution order:
  1. YACINE_API_KEY from the environment — explicit, nothing is written to disk.
  2. A key stored under the data directory, created on first run.

Persisting it (rather than generating a fresh one per launch) is what makes a
double-clicked .exe usable: the key survives restarts, so the UI keeps working
and any saved API client keeps its credential.

The key is safe to embed in the page served at /ui because a cross-origin
attacker cannot read that page — CORS is pinned to this server's own origin,
and the Host header is checked. A page on another site can still SEND requests
here, but without the key they are rejected, which is the point.
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Optional

KEY_FILENAME = "api_key"
_CACHED: Optional[str] = None
_SOURCE: str = ""


def _read_key_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_key_file(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        # Owner-only where the platform supports it.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_api_key(data_dir: Optional[Path] = None, refresh: bool = False) -> str:
    """Return the server's API key, creating and storing one if needed."""
    global _CACHED, _SOURCE
    if _CACHED and not refresh:
        return _CACHED

    env_key = os.environ.get("YACINE_API_KEY", "").strip()
    if env_key:
        _CACHED, _SOURCE = env_key, "environment"
        return _CACHED

    if data_dir is None:
        from .config import get_settings
        data_dir = Path(get_settings().data_dir)
    key_path = Path(data_dir) / KEY_FILENAME

    existing = _read_key_file(key_path)
    if existing:
        _CACHED, _SOURCE = existing, str(key_path)
        return _CACHED

    new_key = secrets.token_urlsafe(32)
    try:
        _write_key_file(key_path, new_key)
        _SOURCE = str(key_path)
    except OSError:
        # Read-only install: fall back to a per-process key. The UI still works
        # because it is served the key directly; external clients must set
        # YACINE_API_KEY instead.
        _SOURCE = "generated for this run only (could not write a key file)"
    _CACHED = new_key
    return _CACHED


def key_source() -> str:
    """Human-readable description of where the active key came from."""
    if not _CACHED:
        get_api_key()
    return _SOURCE


def reset() -> None:
    """Forget the cached key (tests)."""
    global _CACHED, _SOURCE
    _CACHED, _SOURCE = None, ""
