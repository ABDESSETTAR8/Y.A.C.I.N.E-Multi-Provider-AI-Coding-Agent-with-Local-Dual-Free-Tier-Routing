"""OpenAI-compatible provider (LM Studio, vLLM, llama.cpp server, GPT OSS, etc.).

Any server speaking the OpenAI /v1/chat/completions protocol works here.
Just point base_url at it (e.g. LM Studio on http://localhost:1234/v1).
"""
from __future__ import annotations

import json
from typing import Iterator, List, Optional

import httpx

from .base import ModelProvider
from .types import Message, ModelResponse, CompletionChunk


class OpenAICompatProvider(ModelProvider):
    name = "openai"
    supports_tools = True

    def __init__(self, model_config, logger=None) -> None:
        super().__init__(model_config, logger)
        self.base_url = str(self.cfg.base_url).rstrip("/")
        # Accept base_url with or without /v1
        if not self.base_url.endswith("/v1"):
            self.base_url = self.base_url.rstrip("/") + "/v1"

    def is_available(self) -> bool:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{self.base_url}/models",
                          headers=self._headers())
                return r.status_code == 200
        except Exception:
            return False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def complete(self, messages: List[Message], tools=None, **kwargs) -> ModelResponse:
        payload = {
            "model": self.cfg.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": kwargs.get("temperature", self.cfg.temperature),
            "max_tokens": kwargs.get("max_tokens", self.cfg.max_tokens),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        with httpx.Client(timeout=self.cfg.timeout) as c:
            r = c.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()

        choice = data["choices"][0]
        msg = choice["message"]
        text = msg.get("content", "") or ""
        tool_calls = []
        for tc in msg.get("tool_calls", []) or []:
            if tc.get("type") == "function":
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.get("id", f"call_{len(tool_calls)}"),
                    "name": fn.get("name", ""),
                    "arguments": args,
                })
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage=data.get("usage", {}),
            model=self.cfg.model,
            raw=data,
        )

    def stream(self, messages: List[Message], tools=None, **kwargs) -> Iterator[CompletionChunk]:
        payload = {
            "model": self.cfg.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": kwargs.get("temperature", self.cfg.temperature),
            "max_tokens": kwargs.get("max_tokens", self.cfg.max_tokens),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        with httpx.Client(timeout=self.cfg.timeout) as c:
            with c.stream("POST", f"{self.base_url}/chat/completions",
                          json=payload, headers=self._headers()) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        yield CompletionChunk(delta="", done=True)
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = data["choices"][0].get("delta", {}).get("content", "")
                    if delta:
                        yield CompletionChunk(delta=delta, done=False)
