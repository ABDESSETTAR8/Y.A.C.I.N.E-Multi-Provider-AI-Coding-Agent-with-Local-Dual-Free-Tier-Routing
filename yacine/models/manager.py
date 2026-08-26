"""Provider manager: instantiates the configured provider and exposes a
single 'complete' entry point the rest of the engine uses. Switching models
is a config change only — no code changes needed anywhere else.
"""
from __future__ import annotations

from typing import Iterator, List, Optional

from .base import ModelProvider
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider
from .scripted import ScriptedProvider
from .types import Message, ModelResponse, CompletionChunk
from ..core.config import get_settings, ModelConfig


_PROVIDERS = {
    "ollama": OllamaProvider,
    "openai": OpenAICompatProvider,
    "scripted": ScriptedProvider,
}


class ModelManager:
    def __init__(self, model_config: Optional[ModelConfig] = None, logger=None) -> None:
        self.settings = get_settings()
        self.cfg = model_config or self.settings.model
        self.logger = logger
        cls = _PROVIDERS.get(self.cfg.provider, OllamaProvider)
        self.provider: ModelProvider = cls(self.cfg, logger)

    def available(self) -> bool:
        return self.provider.is_available()

    def set_model(self, provider: str, model: str, **overrides) -> None:
        """Hot-swap the active model at runtime (no restart).

        Rebuilds the provider with the new provider/model. ``overrides`` may set
        base_url / api_key / temperature / max_tokens.
        """
        new_cfg = ModelConfig(
            provider=provider,
            model=model,
            base_url=overrides.get("base_url", self.cfg.base_url),
            api_key=overrides.get("api_key", self.cfg.api_key),
            temperature=overrides.get("temperature", self.cfg.temperature),
            max_tokens=overrides.get("max_tokens", self.cfg.max_tokens),
            timeout=overrides.get("timeout", self.cfg.timeout),
            keep_alive=self.cfg.keep_alive,
            script_path=self.cfg.script_path,
        )
        cls = _PROVIDERS.get(provider, OllamaProvider)
        self.cfg = new_cfg
        self.provider = cls(new_cfg, self.logger)
        if self.logger:
            self.logger.info("model", f"switched to {provider}:{model}")

    def complete(self, messages: List[Message], tools=None, **kwargs) -> ModelResponse:
        return self.provider.complete(messages, tools=tools, **kwargs)

    def stream(self, messages: List[Message], tools=None, **kwargs) -> Iterator[CompletionChunk]:
        yield from self.provider.stream(messages, tools=tools, **kwargs)

    def unload(self) -> bool:
        """Evict the provider's current model from VRAM (hot-swap).

        Essential on low-VRAM GPUs so the next agent can load. Returns True if
        the active provider supports explicit unloading and it was attempted.
        """
        return self.provider.unload()

    @property
    def model_name(self) -> str:
        return self.provider.model_name
