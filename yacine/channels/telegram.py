"""Telegram channel bridge for Y.A.C.I.N.E.

Wraps the Telegram Bot API (https://api.telegram.org/bot<TOKEN>/...) with
httpx only — no third-party Telegram library needed. Two jobs:

1. PUBLISH: a `TelegramChannel` is an event sink. The orchestrator forwards its
   `on_event` callbacks here, and the bridge posts to the configured channel/
   group: architect plan, parsed micro-tasks, executioner steps, final summary.
2. COMMAND: `run_polling()` long-polls `getUpdates` and, on
   `/orchestrate <request>`, runs the 4-agent pipeline and streams its output
   back to the channel. `/help` and `/status` are built in.

Token handling: read from TelegramConfig.bot_token or env YACINE_TELEGRAM_TOKEN.
It is sent only to api.telegram.org and never logged.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

import httpx

from ..core.config import TelegramConfig
from ..core.logger import get_logger

API_BASE = "https://api.telegram.org"


class TelegramChannel:
    """Posts Y.A.C.I.N.E pipeline events to a Telegram channel/group."""

    def __init__(self, config: TelegramConfig, logger=None) -> None:
        self.cfg = config
        self.logger = logger or get_logger("telegram")
        self.token = config.bot_token or os.getenv("YACINE_TELEGRAM_TOKEN", "")
        self.chat = config.channel
        self._client = httpx.Client(timeout=30)
        self._last_update = 0

    # -- low-level Bot API ------------------------------------------------
    def _api(self, method: str, payload: Optional[dict] = None) -> dict:
        if not self.token:
            raise RuntimeError("Telegram bot token missing (set token or YACINE_TELEGRAM_TOKEN)")
        url = f"{API_BASE}/bot{self.token}/{method}"
        r = self._client.post(url, json=payload or {})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data

    def send(self, text: str, parse_mode: str = "Markdown") -> dict:
        """Send a message to the channel/group, splitting long ones."""
        if not self.chat:
            self.logger.warning("telegram", "no channel configured; dropping message")
            return {}
        text = text.strip()
        if not text:
            return {}
        # Telegram message cap ~4096; chunk with a footer marker.
        chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [text]
        last = {}
        for i, chunk in enumerate(chunks):
            suffix = f"\n\n_({i+1}/{len(chunks)})_" if len(chunks) > 1 else ""
            try:
                last = self._api("sendMessage", {
                    "chat_id": self.chat,
                    "text": chunk + suffix,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                })
            except Exception as e:  # network blip shouldn't kill the pipeline
                self.logger.warning("telegram", f"send failed: {e}")
            time.sleep(0.3)
        return last

    def send_document(self, path: str, caption: str = "") -> dict:
        if not self.chat:
            return {}
        try:
            with open(path, "rb") as f:
                r = self._client.post(
                    f"{API_BASE}/bot{self.token}/sendDocument",
                    data={"chat_id": self.chat, "caption": caption[:200]},
                    files={"document": f},
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            self.logger.warning("telegram", f"send_document failed: {e}")
            return {}

    # -- event sink (orchestrator forwards here) -------------------------
    def on_event(self, e: dict) -> None:
        """Consume a pipeline event dict and post a human-friendly line."""
        if not self.cfg.stream_to_channel:
            return
        kind = e.get("kind")
        src = e.get("source", "?")
        msg = e.get("message", "")
        label = {
            "architect": "🧠 Architect", "orchestrator": "📋 Orchestrator",
            "executioner": "⚙️ Executioner", "frontend": "💬 Frontend",
            "agent": "▶️", "reasoning": "🤔", "tool": "🔧", "error": "❌",
        }.get(src) or {"agent": "▶️", "reasoning": "🤔", "tool": "🔧",
                       "error": "❌"}.get(kind, "•")
        # Keep channel readable: skip verbose per-tool noise, summarise.
        if kind == "reasoning" and src == "executioner":
            self.send(f"{label} *{src}* step:\n```\n{msg[:1500]}\n```")
        elif kind in ("agent",) or (kind == "reasoning" and src in ("architect", "orchestrator")):
            self.send(f"{label} *{src}*\n{msg[:1500]}")
        elif kind == "error":
            self.send(f"❌ *Error* ({src}): {msg[:1000]}")

    # -- command polling ------------------------------------------------
    def run_polling(self, on_orchestrate: Callable[[str], None]) -> None:
        """Long-poll for commands. `on_orchestrate(request)` runs the pipeline."""
        self._api("getMe")  # auth check
        self.send("🤖 Y.A.C.I.N.E channel online. Send `/orchestrate <request>` to build.")
        offset = 0
        self.logger.info("telegram", "polling started")
        while True:
            try:
                data = self._api("getUpdates", {
                    "offset": offset, "timeout": self.cfg.poll_timeout,
                })
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    self._handle_update(upd, on_orchestrate)
            except Exception as e:
                self.logger.warning("telegram", f"poll error: {e}")
                time.sleep(2)

    def _handle_update(self, upd: dict, on_orchestrate: Callable[[str], None]) -> None:
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        user_id = str(msg.get("from", {}).get("id", ""))
        # If allowed_users set, enforce; channel posts have no 'from'.
        if self.cfg.allowed_users and user_id not in self.cfg.allowed_users:
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "/orchestrate":
            if not arg:
                self.send("Usage: `/orchestrate <what to build>`", parse_mode="Markdown")
                return
            self.send(f"🚀 Running 4-agent pipeline for:\n_{arg}_")
            on_orchestrate(arg)
        elif cmd in ("/status", "/health"):
            self.send("✅ Y.A.C.I.N.E online. Pipeline: Architect→Orchestrator→Executioner→Frontend.")
        elif cmd in ("/help", "/start"):
            self.send("Commands:\n`/orchestrate <request>` — run the pipeline\n"
                      "`/status` — health\n`/help` — this message", parse_mode="Markdown")
        else:
            self.send(f"Unknown command `{cmd}`. Try `/help`.", parse_mode="Markdown")


def build_telegram_channel(logger=None, token: str = "", channel: str = "") -> Optional[TelegramChannel]:
    """Construct a channel from settings; returns None if disabled/unconfigured.
    `token`/`channel` override config (used by CLI --token/--channel)."""
    from ..core.config import get_settings
    cfg = get_settings().telegram
    if token:
        cfg.bot_token = token
        cfg.enabled = True
    if channel:
        cfg.channel = channel
        cfg.enabled = True
    if not cfg.enabled:
        return None
    return TelegramChannel(cfg, logger=logger)
