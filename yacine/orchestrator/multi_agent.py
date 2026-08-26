"""Y.A.C.I.N.E local multi-agent orchestrator — sequential, VRAM-safe.

Why sequential?  The target GPU is an 8 GB RTX 2080 Super. Two 7-8B models
cannot be resident at once, so the orchestrator enforces a strict pipeline:

  Architect (deepseek-r1:7b)
     -> Orchestrator (llama3.1:8b)
        -> Executioner (qwen2.5-coder:7b)   [loops over micro-tasks]
           -> Frontend (qwen2.5:3b)

A single shared ModelManager is used. Before each stage we point its model at
the stage's model and call `manager.unload()` (Ollama keep_alive=0) so the
previous model is evicted from VRAM and only ONE model is ever live. The output
of each stage is passed verbatim as the input to the next (context hand-off),
so no stage re-pays for context it did not produce.

The Executioner loop is special: it consumes the Orchestrator's JSON array and,
for each micro-task, runs the agentic tool loop (read file -> write/modify ->
verify syntax/run) before moving to the next task.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.config import get_settings, ModelConfig
from ..core.logger import get_logger, get_bus
from ..models.manager import ModelManager
from ..models.types import Message
from ..tools.registry import build_tool_registry, make_sandbox
from ..tools.sandbox import Sandbox
from ..agents.agent import Agent
from .prompts import (  # local prompts module
    ARCHITECT_SYSTEM,
    ORCHESTRATOR_SYSTEM,
    EXECUTIONER_SYSTEM,
    FRONTEND_SYSTEM,
)

# The 4-bit quantized roster from the spec. Swap tags here if you pull
# different quantizations; everything else keys off this dict.
MODEL_PLAN: Dict[str, str] = {
    "architect": "deepseek-r1:7b",
    "orchestrator": "llama3.1:8b",
    "executioner": "qwen2.5-coder:7b",
    "frontend": "qwen2.5:3b",
}

DEFAULT_BASE_URL = "http://localhost:11434"


@dataclass
class MicroTask:
    id: str
    action: str
    description: str
    target_files: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class OrchestratorResult:
    task_id: str
    request: str
    success: bool
    plan: str = ""                       # Architect output
    micro_tasks: List[MicroTask] = field(default_factory=list)
    execution_log: str = ""              # Executioner transcript
    summary: str = ""                    # Frontend output
    verification_passed: bool = False
    duration: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "request": self.request,
            "success": self.success,
            "verification_passed": self.verification_passed,
            "duration": round(self.duration, 2),
            "plan": self.plan,
            "micro_tasks": [t.__dict__ for t in self.micro_tasks],
            "execution_log": self.execution_log,
            "summary": self.summary,
            "error": self.error,
        }


class MultiAgentOrchestrator:
    """Runs the 4-stage pipeline sequentially with VRAM hot-swapping."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 models: Optional[Dict[str, str]] = None,
                 workspace_root: Optional[Path] = None,
                 logger=None, on_event: Optional[Callable[[dict], None]] = None,
                 provider: str = "ollama",
                 script_path: Optional[str] = None) -> None:
        self.settings = get_settings()
        self.logger = logger or get_logger("orchestrator")
        self._bus = self.logger.bus if hasattr(self.logger, "bus") else None
        self.on_event = on_event
        self.base_url = base_url
        self.models = dict(MODEL_PLAN)
        if models:
            self.models.update(models)
        # Workspace: prefer the user's chosen project folder; else a temp dir.
        from ..tools.sandbox import Sandbox
        self._task_id = f"orch_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        if workspace_root:
            ws = Path(workspace_root)
        else:
            ws = Path(tempfile.gettempdir()) / self._task_id
        self._workspace_root = ws
        self.sandbox: Sandbox = make_sandbox(str(ws), logger=self.logger)
        self.tools = build_tool_registry(self.sandbox)
        # A SINGLE manager; we mutate its model + provider per stage to guarantee
        # only one model is ever resident. provider stays "ollama".
        self._mgr = ModelManager(
            model_config=ModelConfig(provider=provider, model=self.models["architect"],
                                     base_url=base_url, timeout=self.settings.model.timeout,
                                     keep_alive="0", script_path=script_path),
            logger=self.logger,
        )

    # -- event helper -----------------------------------------------------
    def _emit(self, kind: str, source: str, message: str, **data: object) -> None:
        if self._bus is not None and hasattr(self._bus, "emit"):
            self._bus.emit(kind, source, message, **data)
        if self.on_event:
            try:
                self.on_event({"kind": kind, "source": source,
                               "message": message, "data": data})
            except Exception:
                pass

    def _emit_progress(self, pct: int, stage: str, label: str) -> None:
        """Emit a real progress percentage for the UI (0-100)."""
        self._emit("progress", stage, label, pct=pct, stage=stage)

    # -- model switching + hot-swap --------------------------------------
    def _stage(self, role: str) -> ModelManager:
        """Point the shared manager at `role`'s model, having evicted the previous.

        Enforces sequential execution: we unload() the currently-loaded model
        (keep_alive=0) BEFORE loading the next, so two models are never resident
        simultaneously on the 8 GB GPU.
        """
        # Evict whatever is currently resident (no-op on first stage / scripted).
        try:
            self._mgr.unload()
        except Exception:
            pass
        model = self.models[role]
        self._mgr.cfg.model = model
        self._mgr.cfg.keep_alive = "0"  # evict immediately after each response
        self.logger.info("orchestrator", f"→ stage[{role}] model={model}")
        self._emit("agent", role, f"Loading {model} (sequential hot-swap)")
        return self._mgr

    # -- JSON parsing for the Orchestrator output ------------------------
    @staticmethod
    def _extract_task_array(text: str) -> List[MicroTask]:
        """Pull a JSON array of micro-task objects out of the model output.

        Robust to fences / surrounding prose. Returns [] if nothing parseable.
        """
        if not text:
            return []
        candidates: List[str] = []
        for m in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
            candidates.append(m.strip())
        for m in re.findall(r"(\[.*\])", text, re.DOTALL):
            candidates.append(m.strip())
        candidates.append(text.strip())
        for cand in candidates:
            try:
                obj = json.loads(cand)
            except Exception:
                continue
            if not isinstance(obj, list):
                continue
            tasks: List[MicroTask] = []
            for i, item in enumerate(obj):
                if not isinstance(item, dict):
                    continue
                tasks.append(MicroTask(
                    id=str(item.get("id") or f"T{i+1}"),
                    action=str(item.get("action", "create")),
                    description=str(item.get("description", "")),
                    target_files=list(item.get("target_files") or []),
                    depends_on=list(item.get("depends_on") or []),
                ))
            if tasks:
                return tasks
        return []

    # -- the four stages --------------------------------------------------
    def _run_architect(self, request: str) -> str:
        mgr = self._stage("architect")
        resp = mgr.complete([
            Message(role="system", content=ARCHITECT_SYSTEM),
            Message(role="user", content=f"USER REQUEST:\n{request}"),
        ], keep_alive=0)
        plan = resp.text.strip()
        # Full plan into thinking (UI truncates display); short note to event log.
        self._emit("reasoning", "architect", plan)
        self._emit("agent", "architect", "Plan complete — handing off to Orchestrator")
        return plan

    def _run_orchestrator(self, plan: str) -> List[MicroTask]:
        mgr = self._stage("orchestrator")
        resp = mgr.complete([
            Message(role="system", content=ORCHESTRATOR_SYSTEM),
            Message(role="user",
                    content=f"ARCHITECTURE PLAN:\n{plan}\n\n"
                            f"Convert this into the strict JSON micro-task array."),
        ], keep_alive=0)
        tasks = self._extract_task_array(resp.text)
        # Cap micro-tasks so a sprawling plan can't run for 30+ minutes on 8GB VRAM.
        MAX_TASKS = 6
        if len(tasks) > MAX_TASKS:
            tasks = tasks[:MAX_TASKS]
        task_list = "\n".join(
            f"  {t.id}: [{t.action}] {t.description} -> {', '.join(t.target_files) or 'n/a'}"
            for t in tasks)
        self._emit("reasoning", "orchestrator",
                   f"Parsed {len(tasks)} micro-tasks:\n{task_list}")
        self._emit("agent", "orchestrator", f"{len(tasks)} micro-tasks ready — handing off to Executioner")
        return tasks

    def _run_executioner(self, request: str, tasks: List[MicroTask]) -> str:
        """Execute each micro-task in order with the agentic tool loop."""
        mgr = self._stage("executioner")
        # The Executioner is the ONLY model resident during its whole phase, so
        # it is safe (and far faster) to keep it warm across micro-tasks instead
        # of reloading the 7B weights before every single task. We evict it
        # exactly once, after the loop, before the Frontend 3B loads.
        mgr.cfg.keep_alive = "5m"
        tool_catalog = "\n".join(
            f"- {t.name}: {t.description}" for t in self.tools.all())
        logs: List[str] = []
        passed = True
        done_ids = set()
        total_tasks = len(tasks)
        # Honour dependencies: a task waits until its deps are in done_ids.
        pending = list(tasks)
        while pending:
            if self._cancelled():
                self._emit("agent", "orchestrator", "⏹ Run cancelled by user/server.")
                break
            # Pick the first task whose deps are satisfied.
            progressed = False
            for idx, task in enumerate(pending):
                if all(d in done_ids for d in task.depends_on):
                    pending.pop(idx)
                    progressed = True
                    break
            if not progressed:
                # Circular/unsatisfiable deps: just run the first remaining task.
                task = pending.pop(0)
            self._emit("agent", "executioner", f"[{task.id}] {task.description}")
            self._emit_progress(
                50 + int(40 * len(done_ids) / max(1, total_tasks)),
                "executioner", f"⚙️ Executioner: task {task.id} ({len(done_ids)+1}/{total_tasks})")
            # Context hand-off: feed the original request + this single task.
            agent = Agent(mgr, self.tools, role="executioner",
                          logger=self.logger)
            out = agent.run(
                task=f"ORIGINAL REQUEST: {request}\n\n"
                     f"YOUR CURRENT MICRO-TASK ({task.id}): {task.description}\n"
                     f"TARGET FILES: {', '.join(task.target_files) or 'n/a'}\n\n"
                     f"Focus ONLY on this task. Use tools to write/modify the file(s), "
                     f"then run the build/test command to verify. When it passes, stop.",
                system_extra=EXECUTIONER_SYSTEM.format(TOOLS=tool_catalog),
                max_tool_iters=16,
            )
            log = f"[{task.id}][{task.action}] {task.description}\n" + \
                  "\n".join(f"    {t['tool']}: {str(t['result'].get('output',''))[:200]}"
                            for t in out["tool_log"])
            # Live "thinking" for the Executioner: each tool action it takes.
            for t in out["tool_log"]:
                self._emit("reasoning", "executioner",
                           f"[{task.id}] {t['tool']}: {str(t['result'].get('output',''))[:300]}")
            logs.append(log)
            # If any tool reported an error, mark verification as not clean.
            if any(not t["result"].get("ok", True) for t in out["tool_log"]):
                passed = False
            done_ids.add(task.id)
        self._mgr.unload()  # evict the coder before the 3B frontend loads
        full = "\n".join(logs)
        self._emit("agent", "executioner", f"execution complete (pass={passed})")
        return full

    def _run_frontend(self, request: str, execution_log: str) -> str:
        mgr = self._stage("frontend")
        resp = mgr.complete([
            Message(role="system", content=FRONTEND_SYSTEM),
            Message(role="user",
                    content=f"USER REQUEST:\n{request}\n\n"
                            f"EXECUTION LOGS:\n{execution_log[-4000:]}"),
        ], keep_alive=0)
        return resp.text.strip()

    # -- public entry point ----------------------------------------------
    def run(self, request: str) -> OrchestratorResult:
        start = time.time()
        result = OrchestratorResult(
            task_id=self._task_id, request=request, success=False)
        self._emit("agent", "orchestrator", f"Starting pipeline for: {request[:120]}")
        self._emit_progress(2, "orchestrator", "Pipeline starting…")

        # 1) Architect
        try:
            self._emit_progress(5, "architect", "🧠 Architect: planning the project")
            result.plan = self._run_architect(request)
            self._emit_progress(25, "architect", "🧠 Architect: plan ready")
        except Exception as e:
            result.error = f"architect failed: {e}"
            result.duration = time.time() - start
            self._emit_progress(100, "architect", "❌ Architect failed")
            return result

        # 2) Orchestrator
        try:
            self._emit_progress(30, "orchestrator", "📋 Orchestrator: breaking plan into tasks")
            result.micro_tasks = self._run_orchestrator(result.plan)
            self._emit_progress(45, "orchestrator", f"📋 Orchestrator: {len(result.micro_tasks)} tasks")
        except Exception as e:
            result.error = f"orchestrator failed: {e}"
            result.duration = time.time() - start
            self._emit_progress(100, "orchestrator", "❌ Orchestrator failed")
            return result

        if not result.micro_tasks:
            result.error = ("orchestrator produced no parseable micro-tasks "
                            "(JSON contract violated)")
            result.duration = time.time() - start
            self._emit_progress(100, "orchestrator", "❌ No micro-tasks")
            return result

        # 3) Executioner (the loop)
        try:
            self._emit_progress(50, "executioner", "⚙️ Executioner: starting tasks")
            result.execution_log = self._run_executioner(request, result.micro_tasks)
            self._emit_progress(90, "executioner", "⚙️ Executioner: tasks complete")
        except Exception as e:
            result.error = f"executioner failed: {e}"
            result.duration = time.time() - start
            self._emit_progress(100, "executioner", "❌ Executioner failed")
            return result

        # 4) Frontend summary
        try:
            self._emit_progress(95, "frontend", "💬 Frontend: summarizing results")
            result.summary = self._run_frontend(request, result.execution_log)
        except Exception as e:
            result.summary = "(frontend summary unavailable)"
            result.error = f"frontend failed: {e}"

        result.verification_passed = "Traceback" not in result.execution_log and \
            ("exit=0" in result.execution_log or "OK" in result.execution_log)
        result.success = bool(result.execution_log)
        result.duration = time.time() - start
        self._emit_progress(100, "frontend", "✅ Pipeline complete")
        self._emit("agent", "orchestrator",
                   f"Pipeline done in {result.duration:.1f}s")
        return result

    def request_cancel(self) -> None:
        """Set a flag the run loop checks so a long pipeline can be stopped."""
        self._cancel = True

    def _cancelled(self) -> bool:
        return getattr(self, "_cancel", False)
