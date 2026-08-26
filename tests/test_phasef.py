"""Tests for Phase F: long-term memory, vision, voice (all offline-safe)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yacine.memory.longterm import LongTermMemory, make_embedder, memory_tokens
from yacine.tools.sandbox import Sandbox
from yacine.core.security import SecurityEnforcer
from yacine.tools.vision import VisionTool
from yacine.tools.voice import TextToSpeechTool, SpeechToTextTool


def test_embedder_fallback():
    emb = make_embedder("hashed")
    v = emb.embed("hello world")
    assert len(v) > 0
    # sentence-transformers not guaranteed; just assert it doesn't crash on 'auto'
    auto = make_embedder("auto")
    assert hasattr(auto, "embed")


def test_longterm_memory_and_consolidation(tmp_path):
    mem = LongTermMemory(data_dir=tmp_path, embedder_pref="hashed")
    for i in range(5):
        mem.add(f"User prefers pytest for testing, note {i}", category="fact")
    mem.add("Completely unrelated memory about cats", category="fact")
    before = len(mem.all())
    removed = mem.consolidate()
    # The 5 near-duplicate facts should be merged down.
    assert removed >= 1
    assert len(mem.all()) < before


def test_vision_metadata_fallback(tmp_path):
    from yacine.tools.sandbox import Sandbox
    from yacine.core.security import SecurityEnforcer
    sandbox = Sandbox(tmp_path / "ws", security=SecurityEnforcer())
    # Write a tiny fake PNG (not a real image; dims will be empty but metadata works).
    png = sandbox.workspace / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n fakebytes")
    tool = VisionTool(sandbox)
    res = tool.execute({"path": "x.png"})
    assert res.ok  # fallback metadata, never crash
    assert "sha" in res.output


def test_voice_unavailable_graceful(tmp_path):
    from yacine.tools.sandbox import Sandbox
    from yacine.core.security import SecurityEnforcer
    sandbox = Sandbox(tmp_path / "ws", security=SecurityEnforcer())
    tts = TextToSpeechTool(sandbox)
    res = tts.execute({"text": "hello"})
    assert res.ok is False  # edge-tts not installed -> graceful
    stt = SpeechToTextTool(sandbox)
    (sandbox.workspace / "a.wav").write_bytes(b"RIFF....")
    r2 = stt.execute({"path": "a.wav"})
    assert r2.ok is False  # whisper not installed -> graceful
