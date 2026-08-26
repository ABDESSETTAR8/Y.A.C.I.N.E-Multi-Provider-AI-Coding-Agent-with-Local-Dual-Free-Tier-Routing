"""Y.A.C.I.N.E Plugin SDK.

Discover, load and activate third-party extensions without risking the host
engine. See :mod:`yacine.plugins.loader` for the manifest format and hooks.
"""
from __future__ import annotations

from .loader import Plugin, discover_plugins, load_all_plugins

__all__ = ["Plugin", "discover_plugins", "load_all_plugins"]
