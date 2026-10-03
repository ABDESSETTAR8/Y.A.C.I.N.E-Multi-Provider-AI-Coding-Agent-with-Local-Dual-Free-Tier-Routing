"""Provider manager: instantiates the configured provider and exposes a
single 'complete' entry point the rest of the engine uses. Switching models
is a config change only — no code changes needed anywhere else.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Iterator, List, Optional

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

    # Reachability is polled by the UI; without a cache every poll adds an HTTP
    # round trip to Ollama (and a 5s stall whenever it is down).
    _AVAILABILITY_TTL = 10.0

    def available(self, max_age: float = None) -> bool:
        """Is the backend reachable? Cached briefly to keep polling cheap."""
        ttl = self._AVAILABILITY_TTL if max_age is None else max_age
        now = time.monotonic()
        cached = getattr(self, "_avail_cache", None)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]
        ok = self.provider.is_available()
        self._avail_cache = (now, ok)
        return ok

    def set_model(self, provider: str, model: str, **overrides) -> None:
        """Hot-swap the active model at runtime (no restart).

        Rebuilds the provider with the new provider/model. ``overrides`` may set
        base_url / api_key / temperature / max_tokens / timeout / keep_alive /
        num_ctx.

        keep_alive and num_ctx MUST be honoured here. They were silently
        dropped before, with two costs the user feels directly:

          * The chat tab asks for keep_alive="15m" so the small model stays
            resident between turns. Dropping it meant the pipeline's
            keep_alive="0" (evict immediately) stayed in force, so every single
            chat message paid a full model load from disk — seconds per reply.
          * The chat model asks for a smaller num_ctx. Dropping it ran chat at
            the heavy coder model's window, making every prompt slower to
            prefill and using VRAM the GPU does not have to spare.

        Rebuilding also becomes a no-op when nothing changed, so a repeated
        switch to the model already active does not churn the provider.
        """
        cfg_kwargs = dict(
            provider=provider,
            model=model,
            base_url=overrides.get("base_url", self.cfg.base_url),
            api_key=overrides.get("api_key", self.cfg.api_key),
            temperature=overrides.get("temperature", self.cfg.temperature),
            max_tokens=overrides.get("max_tokens", self.cfg.max_tokens),
            timeout=overrides.get("timeout", self.cfg.timeout),
            keep_alive=overrides.get("keep_alive", self.cfg.keep_alive),
            num_ctx=overrides.get("num_ctx", getattr(self.cfg, "num_ctx", None)),
            script_path=self.cfg.script_path,
        )
        cfg_kwargs = {k: v for k, v in cfg_kwargs.items() if v is not None}
        new_cfg = ModelConfig(**cfg_kwargs)
        if (self.cfg.provider == new_cfg.provider
                and self.cfg.model == new_cfg.model
                and self.cfg.base_url == new_cfg.base_url
                and self.cfg.keep_alive == new_cfg.keep_alive
                and getattr(self.cfg, "num_ctx", None) == getattr(new_cfg, "num_ctx", None)
                and self.cfg.max_tokens == new_cfg.max_tokens
                and self.cfg.temperature == new_cfg.temperature):
            return  # already active — rebuilding would just churn
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


# --------------------------------------------------------------------------
# Multi-provider manager: routes requests by role to different providers.
#
# Free-tier optimization: pairs Laguna 2.1 (deep reasoning / coding) with
# Gemini (review / test-gen / docs) so both free quotas are consumed in
# complementarity rather than one model doing work the other could do cheaper.
# --------------------------------------------------------------------------
class MultiProviderManager:
    """Routes to different providers based on the pipeline role.

    Usage:
        mm = MultiProviderManager()
        architect_mgr = mm.get_for_role("architect")   # Laguna
        reviewer_mgr  = mm.get_for_role("reviewer")     # Gemini
        resp = architect_mgr.complete(messages)
    """

    # Default role → model mapping (can be overridden by config).
    DEFAULT_PLAN: Dict[str, str] = {
        "architect":    "poolside/laguna-s-2.1:free",
        "orchestrator": "poolside/laguna-s-2.1:free",
        "executioner":  "poolside/laguna-s-2.1:free",
        "frontend":     "poolside/laguna-s-2.1:free",
        "reviewer":     "gpt-2.0-flash",
        "testgen":      "gpt-2.0-flash",
        "chat":         "poolside/laguna-s-2.1:free",
    }

    def __init__(self, settings=None, logger=None) -> None:
        from ..core.config import get_settings
        self.settings = settings or get_settings()
        self.logger = logger

        # Ollama (local Laguna 2.1)
        ollama_cfg = ModelConfig(
            provider="ollama",
            model=self.DEFAULT_PLAN["architect"],
            base_url=self.settings.model.base_url,
            keep_alive="0",
            num_ctx=8192,
            timeout=self.settings.model.timeout,
        )

        # Gemini via OpenAI-compatible endpoint
        gemini_cfg = ModelConfig(
            provider="openai",
            model=self.DEFAULT_PLAN["reviewer"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.environ.get("GOOGLE_API_KEY", ""),
            keep_alive="0",
            num_ctx=8192,
            timeout=self.settings.model.timeout,
        )

        self._providers: Dict[str, ModelManager] = {
            "ollama": ModelManager(model_config=ollama_cfg, logger=logger),
            "gemini": ModelManager(model_config=gemini_cfg, logger=logger),
        }
        self._role_map: Dict[str, str] = dict(self.DEFAULT_PLAN)
        # Allow settings to override the role→model mapping.
        override = getattr(self.settings, "model_role_map", None)
        if override:
            self._role_map.update(override)

    def get_for_role(self, role: str) -> ModelManager:
        """Return the ModelManager that should handle ``role``."""
        model_name = self._role_map.get(role, self.DEFAULT_PLAN.get(role, self.DEFAULT_PLAN["architect"]))
        # Decide provider by model name convention:
        # poolside/* or known ollama tags → ollama; everything else → gemini.
        if model_name.startswith("poolside/") or model_name.startswith("qwen") or model_name.startswith("llama") or model_name.startswith("deepseek"):
            return self._providers["ollama"]
        return self._providers["gemini"]

    def set_role_model(self, role: str, model: str) -> None:
        """Override which model a particular role uses at runtime."""
        self._role_map[role] = model
        if self.logger:
            self.logger.info("model", f"role '{role}' → {model}")

    def all_models(self) -> List[dict]:
        """Describe every configured provider/model for the /models endpoint."""
        return [
            {"role": role, "provider": pm.cfg.provider, "model": pm.cfg.model}
            for role, pm in (
                (r, self.get_for_role(r)) for r in self._role_map
            )
        ]

    @property
    def active(self) -> ModelManager:
        """Default active manager (the architect / Laguna provider)."""
        return self._providers["ollama"]
