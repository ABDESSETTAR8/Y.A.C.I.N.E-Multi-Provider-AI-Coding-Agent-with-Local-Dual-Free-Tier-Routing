"""Structured logging + telemetry for Y.A.C.I.N.E.

Every action, reasoning step, tool call, error and fix is recorded both to a
rotating log file and to an in-memory ring buffer that the UI/API can stream.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import get_settings

# --------------------------------------------------------------------------
# Event bus (loose coupling between engine and UI/API)
# --------------------------------------------------------------------------
@dataclass
class Event:
    kind: str               # "reasoning" | "tool" | "error" | "fix" | "info" | "agent" | "metric"
    source: str             # where it came from (e.g. agent name, "loop", "system")
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "message": self.message,
            "data": self.data,
            "ts": self.ts,
            "iso": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat(),
        }


class EventBus:
    """Simple pub/sub used to stream events to UI / API / logs."""

    def __init__(self) -> None:
        self._subscribers: List[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                # A misbehaving subscriber must never break the engine.
                pass

    def emit(self, kind: str, source: str, message: str, **data: Any) -> None:
        self.publish(Event(kind=kind, source=source, message=message, data=data))

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Event:
        return Event(
            kind=d.get("kind", "info"),
            source=d.get("source", "system"),
            message=d.get("message", ""),
            data=d.get("data", {}),
            ts=d.get("ts", time.time()),
        )


# --------------------------------------------------------------------------
# Logger
# --------------------------------------------------------------------------
class YacineLogger:
    def __init__(self, name: str = "yacine", bus: Optional[EventBus] = None) -> None:
        self.name = name
        self.bus = bus or EventBus()
        settings = get_settings()
        self.log_path = settings.logs_dir / "yacine.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(f"yacine.{name}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        if not self._logger.handlers:
            fh = logging.FileHandler(self.log_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
            fh.setFormatter(fmt)
            self._logger.addHandler(fh)
        self._lock = threading.Lock()

    def _write(self, level: str, source: str, message: str, **data: Any) -> None:
        line = f"[{source}] {message}"
        getattr(self._logger, level)(line)
        # Emit an event for live consumers.
        kind = "error" if level in ("error", "critical") else (
            "info" if level in ("warning", "info") else "reasoning")
        self.bus.emit(kind, source, message, level=level, **data)

    def debug(self, source: str, message: str, **d: Any) -> None:
        self._write("debug", source, message, **d)

    def info(self, source: str, message: str, **d: Any) -> None:
        self._write("info", source, message, **d)

    def warning(self, source: str, message: str, **d: Any) -> None:
        self._write("warning", source, message, **d)

    def error(self, source: str, message: str, **d: Any) -> None:
        self._write("error", source, message, **d)

    def tool(self, source: str, message: str, **d: Any) -> None:
        self.bus.emit("tool", source, message, **d)
        self._logger.info(f"[TOOL][{source}] {message}")

    def metric(self, source: str, message: str, **d: Any) -> None:
        self.bus.emit("metric", source, message, **d)

    def reasoning(self, source: str, message: str, **d: Any) -> None:
        self.bus.emit("reasoning", source, message, **d)

    def agent(self, source: str, message: str, **d: Any) -> None:
        self.bus.emit("agent", source, message, **d)


# Default shared bus + logger
_default_bus = EventBus()
_default_logger = YacineLogger(bus=_default_bus)


def get_logger(name: str = "yacine") -> YacineLogger:
    return YacineLogger(name=name, bus=_default_bus)


def get_bus() -> EventBus:
    return _default_bus
