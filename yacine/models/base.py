"""Abstract base class for model providers.

A provider turns a list of Messages (+ optional tools) into a ModelResponse,
and can optionally stream tokens.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, List

from .types import Message, ModelResponse, CompletionChunk


class ModelProvider(ABC):
    name: str = "base"
    supports_tools: bool = False

    def __init__(self, model_config, logger=None) -> None:
        self.cfg = model_config
        self.logger = logger

    @abstractmethod
    def complete(self, messages: List[Message], tools=None, **kwargs) -> ModelResponse:
        ...

    def stream(self, messages: List[Message], tools=None, **kwargs) -> Iterator[CompletionChunk]:
        """Default: non-streaming wrapped as a single chunk."""
        resp = self.complete(messages, tools=tools, **kwargs)
        yield CompletionChunk(delta=resp.text, done=True)

    def is_available(self) -> bool:
        """Return True if the provider backend is reachable."""
        return True

    @property
    def model_name(self) -> str:
        return self.cfg.model

    def unload(self) -> bool:
        """Best-effort: free this backend's VRAM so the next agent can load.

        Returns True if the backend supports an explicit unload and it was
        attempted. On backends that cannot unload (or are stateless) it is a
        no-op returning False. The Ollama provider implements this via
        `keep_alive: 0` (hot-swap), which is essential on low-VRAM GPUs
        (e.g. an 8 GB RTX 2080 Super) where two models cannot be resident at
        once. The Orchestrator calls unload() between every stage to prevent
        OOM.
        """
        return False
