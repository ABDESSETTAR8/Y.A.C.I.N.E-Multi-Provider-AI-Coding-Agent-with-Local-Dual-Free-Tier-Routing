"""Y.A.C.I.N.E channel bridges (Telegram, etc.).

A "channel" is an external surface Y.A.C.I.N.E posts pipeline results to and can
receive commands from. The Telegram bridge uses the Bot API over HTTP (no
telethon/aiogram dependency — just httpx, which the project already has).
"""
from __future__ import annotations

from .telegram import TelegramChannel, build_telegram_channel

__all__ = ["TelegramChannel", "build_telegram_channel"]
