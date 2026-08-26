"""Planner: generates a structured implementation plan.

The spec requires, before writing code, a plan covering:
requirements, architecture, dependencies, risks, edge cases, performance,
security, estimated files, implementation steps, testing strategy, rollback.

The Planner persona (PM/Architect) produces this; the autonomous engine then
executes the steps and tracks progress against the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..models.types import Message
from ..models.manager import ModelManager
from .personas import PERSONAS


@dataclass
class PlanStep:
    index: int
    description: str
    done: bool = False
    result: str = ""


@dataclass
class Plan:
    goal: str = ""
    requirements: List[str] = field(default_factory=list)
    architecture: str = ""
    dependencies: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    edge_cases: List[str] = field(default_factory=list)
    performance: str = ""
    security: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    steps: List[PlanStep] = field(default_factory=list)
    testing_strategy: str = ""
    rollback: str = ""

    def to_markdown(self) -> str:
        lines = [f"# Plan: {self.goal}", ""]
        if self.requirements:
            lines.append("## Requirements\n" + "\n".join(f"- {r}" for r in self.requirements))
        if self.architecture:
            lines.append(f"## Architecture\n{self.architecture}")
        if self.dependencies:
            lines.append("## Dependencies\n" + "\n".join(f"- {d}" for d in self.dependencies))
        if self.risks:
            lines.append("## Risks\n" + "\n".join(f"- {r}" for r in self.risks))
        if self.edge_cases:
            lines.append("## Edge Cases\n" + "\n".join(f"- {e}" for e in self.edge_cases))
        if self.security:
            lines.append("## Security\n" + "\n".join(f"- {s}" for s in self.security))
        if self.files:
            lines.append("## Estimated Files\n" + "\n".join(f"- {f}" for f in self.files))
        if self.steps:
            lines.append("## Implementation Steps")
            for s in self.steps:
                mark = "x" if s.done else " "
                lines.append(f"- [{mark}] {s.index}. {s.description}")
        if self.testing_strategy:
            lines.append(f"## Testing Strategy\n{self.testing_strategy}")
        if self.rollback:
            lines.append(f"## Rollback\n{self.rollback}")
        return "\n".join(lines)

    def mark_step(self, index: int, result: str = "") -> None:
        for s in self.steps:
            if s.index == index:
                s.done = True
                s.result = result

    def progress(self) -> str:
        done = sum(1 for s in self.steps if s.done)
        total = len(self.steps)
        return f"{done}/{total}"


def generate_plan(manager: ModelManager, request: str, context_block: str,
                  role: str = "pm", logger=None) -> str:
    """Ask the planner persona for a plan as markdown text.

    Returns the raw markdown. Parsing into a Plan object is best-effort and
    optional; the engine can also just carry the markdown around.
    """
    system = PERSONAS.get(role, PERSONAS["pm"])
    user = (
        f"USER REQUEST:\n{request}\n\n"
        f"REPOSITORY CONTEXT:\n{context_block}\n\n"
        "Produce a structured plan with these sections exactly as headings:\n"
        "Requirements, Architecture, Dependencies, Risks, Edge Cases, "
        "Performance, Security, Estimated Files, Implementation Steps, "
        "Testing Strategy, Rollback Strategy.\n"
        "Keep each section concise and actionable."
    )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]
    resp = manager.complete(messages)
    if logger:
        logger.reasoning("planner", resp.text[:500])
    return resp.text
