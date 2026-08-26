"""Y.A.C.I.N.E — Local Multi-Agent Orchestrator.

A strictly sequential 4-agent pipeline for the 8 GB RTX 2080 Super: only ONE
model is ever resident in VRAM at a time, and each is evicted (keep_alive=0 /
unload()) before the next loads, so two models can never collide and OOM.

Pipeline (context hand-off, zero redundant context cost):
  Architect (deepseek-r1:7b)  -> high-level plan
        |> Orchestrator (llama3.3:8b) -> strict JSON micro-task array
              |> Executioner (qwen2.5-coder:7b) -> loops tasks, writes + verifies code
                    |> Frontend (qwen2.5:3b) -> human-friendly summary

Usage:
  from yacine.orchestrator import MultiAgentOrchestrator
  orch = MultiAgentOrchestrator()
  result = orch.run("Build a Flask todo API with SQLite")
  print(result.summary)
"""
from __future__ import annotations

from .multi_agent import (
    MultiAgentOrchestrator,
    OrchestratorResult,
    MicroTask,
    MODEL_PLAN,
    DEFAULT_BASE_URL,
)

__all__ = [
    "MultiAgentOrchestrator",
    "OrchestratorResult",
    "MicroTask",
    "MODEL_PLAN",
    "DEFAULT_BASE_URL",
]
