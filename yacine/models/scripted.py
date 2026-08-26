"""Scripted / offline provider.

Lets the entire autonomous engine run WITHOUT a real model. This is used for:
  - `yacine chat --script script.yaml`   (offline demo / reproducible runs)
  - The test-suite, which proves the self-healing loop completes a real task.

A script is a YAML/JSON file: a list of "turns". Each turn:
  match:   optional regex tested against the LATEST tool result (None = always)
  text:    the assistant message text to emit
  tool_calls:  optional list of {name, arguments}

The provider walks the turns in order, skipping any turn whose `match` does
not match the latest tool result. This makes branching (e.g. "if the tests
failed, emit the fix") possible for deterministic self-healing demos.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

import yaml

from .base import ModelProvider
from .types import Message, ModelResponse, CompletionChunk


class ScriptedProvider(ModelProvider):
    name = "scripted"
    supports_tools = True

    def __init__(self, model_config, logger=None) -> None:
        super().__init__(model_config, logger)
        self._idx = 0
        self._turns: List[dict] = []
        self._raw_script: Optional[str] = None
        # Observation channel: the engine feeds the latest tool/verify output
        # here so `match` can branch on it even when an agent builds fresh
        # messages (no tool-role history). This is what makes self-heal
        # deterministic in scripted mode.
        self._observation: str = ""
        if model_config.script_path:
            sp = Path(model_config.script_path)
            if not sp.is_absolute():
                # Resolve relative to the app base (frozen .exe folder or repo root).
                import sys
                base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
                    else Path(__file__).resolve().parents[2]
                sp = base / sp
            self.load_script(sp)

    def observe(self, text: str) -> None:
        """Record the latest external observation (e.g. test output) for matching."""
        self._observation = text or ""

    def reset(self) -> None:
        self._idx = 0
        self._observation = ""

    # -- scripting API ----------------------------------------------------
    def load_script(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        self._raw_script = text
        if path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        self._turns = data if isinstance(data, list) else data.get("turns", [])
        self._idx = 0

    def set_turns(self, turns: List[dict]) -> None:
        self._turns = turns
        self._idx = 0

    def _latest_tool_result(self, messages: List[Message]) -> str:
        for m in reversed(messages):
            if m.role == "tool":
                return m.content
        # Fall back to the engine-provided observation (scripted self-heal).
        return getattr(self, "_observation", "")

    def set_observation(self, text: str) -> None:
        self._observation = text or ""

    # -- provider interface ----------------------------------------------
    def complete(self, messages: List[Message], tools=None, **kwargs) -> ModelResponse:
        latest = self._latest_tool_result(messages)
        # Find the next applicable turn.
        start = self._idx
        chosen = None
        for i in range(start, len(self._turns)):
            turn = self._turns[i]
            match = turn.get("match")
            if match is None or re.search(match, latest):
                chosen = turn
                self._idx = i + 1
                break
        if chosen is None:
            # No more scripted turns -> graceful stop.
            return ModelResponse(
                text="[scripted provider: no further turns; stopping]",
                tool_calls=[], model=self.cfg.model)

        text = chosen.get("text", "")
        tool_calls = []
        for tc in chosen.get("tool_calls", []) or []:
            tool_calls.append({
                "id": f"call_{len(tool_calls)}_{self._idx}",
                "name": tc["name"],
                "arguments": tc.get("arguments", {}),
            })
        return ModelResponse(
            text=text, tool_calls=tool_calls,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            model=self.cfg.model, raw={"scripted": True})

    def stream(self, messages: List[Message], tools=None, **kwargs):
        resp = self.complete(messages, tools=tools, **kwargs)
        yield CompletionChunk(delta=resp.text, done=True)
