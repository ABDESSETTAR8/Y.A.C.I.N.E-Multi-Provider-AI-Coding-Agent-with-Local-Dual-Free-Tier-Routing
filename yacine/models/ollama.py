"""Ollama provider (primary per the Y.A.C.I.N.E spec: Qwen2.5-Coder).

Uses the local Ollama HTTP API (/api/chat). Supports tool calling and
streaming. Falls back gracefully when the server is unavailable.
"""
from __future__ import annotations

import json
from typing import Iterator, List, Optional

import httpx

from .base import ModelProvider
from .types import Message, ModelResponse, CompletionChunk


def _to_ollama_messages(messages: List[Message]) -> List[dict]:
    out = []
    for m in messages:
        entry: dict = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": tc.get("name", ""),
                              "arguments": tc.get("arguments", {})}}
                for tc in m.tool_calls
            ]
        if m.role == "tool" and m.name:
            entry["name"] = m.name
        out.append(entry)
    return out


def _to_ollama_tools(tools) -> Optional[List[dict]]:
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            },
        })
    return out


class OllamaProvider(ModelProvider):
    name = "ollama"
    supports_tools = True

    def __init__(self, model_config, logger=None) -> None:
        super().__init__(model_config, logger)
        self.base_url = str(self.cfg.base_url).rstrip("/")
        # keep_alive for normal generations: how long Ollama holds the model in
        # VRAM *after* the response. On an 8 GB GPU we default to a short window
        # so the next agent can hot-swap in. Override per-call via kwargs.
        self.keep_alive = getattr(model_config, "keep_alive", None)
        if self.keep_alive is None:
            self.keep_alive = "5m"  # keep briefly for cheap follow-up turns

    def is_available(self) -> bool:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    def _keep_alive(self, kwargs) -> object:
        """Resolve the keep_alive for this call.

        Priority: explicit per-call kwarg -> provider default (self.keep_alive)
        -> Ollama server default (omit). Accepts int seconds, human strings
        ("5m", "30s"), or 0 to unload immediately.
        """
        if "keep_alive" in kwargs:
            return kwargs.pop("keep_alive")
        if self.keep_alive is not None:
            return self.keep_alive
        return None  # let the server decide

    def _options(self, kwargs: dict) -> dict:
        """Sampling options, including the context window.

        num_ctx matters more than it looks: without it Ollama falls back to its
        own default window (4096 on current builds, 2048 on older ones) and
        SILENTLY discards whatever overflows from the front of the
        conversation. A coder agent sends a context block, a plan and
        accumulated tool output, so the part that gets dropped is usually the
        system prompt and the instructions — which reads to the user as a model
        that ignores what it was told.
        """
        opts = {
            "temperature": kwargs.get("temperature", self.cfg.temperature),
            "num_predict": kwargs.get("max_tokens", self.cfg.max_tokens),
        }
        num_ctx = kwargs.get("num_ctx", getattr(self.cfg, "num_ctx", None))
        if num_ctx:
            opts["num_ctx"] = int(num_ctx)
        return opts

    def complete(self, messages: List[Message], tools=None, **kwargs) -> ModelResponse:
        keep_alive = self._keep_alive(kwargs)
        payload: dict = {
            "model": self.cfg.model,
            "messages": _to_ollama_messages(messages),
            "stream": False,
            "options": self._options(kwargs),
        }
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        otools = _to_ollama_tools(tools)
        if otools:
            payload["tools"] = otools

        with httpx.Client(timeout=self.cfg.timeout) as c:
            r = c.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()

        msg = data.get("message", {})
        text = msg.get("content", "")
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            tool_calls.append({
                "id": tc.get("id", f"call_{len(tool_calls)}"),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", {}),
            })
        usage = data.get("prompt_eval_count", 0)
        # Make truncation visible instead of silent.
        window = int(getattr(self.cfg, "num_ctx", 0) or 0)
        if window and usage and usage >= window * 0.9 and self.logger:
            self.logger.error(
                "ollama",
                f"Prompt used {usage} of {window} context tokens — Ollama drops "
                f"the overflow from the front of the conversation. Raise num_ctx "
                f"or shorten the context.")
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage={"prompt_tokens": usage, "completion_tokens": data.get("eval_count", 0)},
            model=self.cfg.model,
            raw=data,
        )

    def stream(self, messages: List[Message], tools=None, **kwargs) -> Iterator[CompletionChunk]:
        keep_alive = self._keep_alive(kwargs)
        payload: dict = {
            "model": self.cfg.model,
            "messages": _to_ollama_messages(messages),
            "stream": True,
            "options": self._options(kwargs),
        }
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        otools = _to_ollama_tools(tools)
        if otools:
            payload["tools"] = otools
        with httpx.Client(timeout=self.cfg.timeout) as c:
            with c.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                full = []
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("message", {}).get("content", "")
                    if delta:
                        full.append(delta)
                        yield CompletionChunk(delta=delta, done=False)
                    if data.get("done"):
                        yield CompletionChunk(delta="", done=True)
                        break

    def unload(self) -> bool:
        """Hot-swap: evict this model from VRAM immediately (keep_alive: 0).

        On an 8 GB GPU this is what lets the next agent load without OOM. We
        POST to /api/generate with an empty prompt and keep_alive=0, which
        tells Ollama to drop the model right after the (trivial) response.
        Returns True when the unload request was accepted.
        """
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.cfg.model, "prompt": "", "keep_alive": 0},
                )
                ok = r.status_code == 200
            if self.logger:
                self.logger.info("ollama", f"unload {self.cfg.model} -> {'ok' if ok else 'fail'}")
            return ok
        except Exception as e:
            if self.logger:
                self.logger.warning("ollama", f"unload {self.cfg.model} failed: {e}")
            return False
