"""Voice tools (Phase F2).

Text-to-speech and speech-to-text via local engines, with graceful fallback:
  - TTS: edge-tts (free, online) when installed; otherwise reports unavailable.
  - STT: whisper.cpp / openai-whisper when installed; otherwise unavailable.

The agent can call these to speak responses or transcribe voice notes. All paths
are sandbox-scoped. Never crashes if engines are missing.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import Tool, ToolResult
from .sandbox import Sandbox


class TextToSpeechTool(Tool):
    name = "text_to_speech"
    description = ("Convert text to speech and save an audio file (mp3/wav) in the workspace. "
                  "Uses edge-tts when installed. Returns the audio path.")
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to speak."},
            "voice": {"type": "string", "description": "Voice name (default en-US-AriaNeural)."},
            "out": {"type": "string", "description": "Output filename (e.g. out.mp3)."},
        },
        "required": ["text"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, text: str, voice: str = "en-US-AriaNeural", out: str = "speech.mp3") -> ToolResult:
        try:
            import edge_tts  # type: ignore
        except ImportError:
            return ToolResult(ok=False, output="edge-tts not installed (pip install edge-tts). TTS unavailable.")
        dest = self.sandbox.workspace / out
        try:
            import asyncio
            async def _speak():
                comm = edge_tts.Communicate(text, voice)
                await comm.save(str(dest))
            asyncio.run(_speak())
            return ToolResult(ok=True, output=f"Saved audio to {out}", data={"path": out})
        except Exception as e:
            return ToolResult(ok=False, output=f"TTS failed: {e}")


class SpeechToTextTool(Tool):
    name = "speech_to_text"
    description = ("Transcribe an audio file in the workspace to text using whisper when "
                  "available. Returns the transcript.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Audio file path relative to workspace."},
            "model": {"type": "string", "description": "Whisper model size (default base)."},
        },
        "required": ["path"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str, model: str = "base") -> ToolResult:
        fp = self.sandbox.workspace / path
        if not fp.exists():
            return ToolResult(ok=False, output=f"Audio not found: {path}")
        # Prefer openai-whisper
        try:
            import whisper  # type: ignore
            m = whisper.load_model(model)
            res = m.transcribe(str(fp))
            return ToolResult(ok=True, output=res.get("text", ""), data={"text": res.get("text", "")})
        except ImportError:
            pass
        # Fallback: whisper.cpp CLI
        try:
            r = subprocess.run(["whisper-cli", "--model", model, str(fp)],
                              capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                return ToolResult(ok=True, output=r.stdout)
        except Exception:
            pass
        return ToolResult(ok=False, output="No STT engine installed (pip install openai-whisper). STT unavailable.")
