"""Shared message types used across the model/agent layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    # Tool calls an ASSISTANT turn requested, as
    # [{"id": ..., "name": ..., "arguments": {...}}]. Without this the
    # transcript contains tool results with nothing that asked for them, which
    # OpenAI-compatible backends reject outright and which costs local models
    # the record of what they requested.
    tool_calls: List[Dict[str, object]] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        d: Dict[str, object] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d


@dataclass
class ModelResponse:
    text: str
    tool_calls: List[Dict[str, object]] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw: object = None


@dataclass
class CompletionChunk:
    """A streaming token chunk."""
    delta: str
    done: bool = False
