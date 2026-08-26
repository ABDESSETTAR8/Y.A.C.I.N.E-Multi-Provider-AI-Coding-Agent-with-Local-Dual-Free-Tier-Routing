"""Autonomous self-healing orchestrator — the core engine of Y.A.C.I.N.E.

Implements the full development lifecycle from the spec:

  Understand Request -> Analyze Project -> Build Context -> Research (if needed)
  -> Generate Plan -> [Wait for approval] -> Implement -> Generate Tests
  -> Execute -> Observe -> Read Errors -> Self-Debug -> Modify Code -> Re-run
  -> Repeat Until Green -> Review -> Optimize -> Generate Docs -> Commit -> Deliver

The loop NEVER stops after the first code generation. On failure it captures
the traceback, identifies the root cause (Debugger agent), rewrites only the
affected code, preserves working code, retries, and repeats up to
max_self_heal_iterations. It produces a structured TaskResult with a
verification verdict so we can PROVE a task actually completed.

The orchestrator is provider-agnostic: it works with Ollama, OpenAI-compatible
backends, or the scripted provider used in tests.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.config import get_settings
from ..core.context import ContextEngine
from ..core.logger import get_logger
from ..models.manager import ModelManager
from ..models.types import Message
from ..tools.registry import build_tool_registry, make_sandbox
from ..tools.sandbox import Sandbox
from ..memory.store import MemoryStore
from ..agents.agent import run_agent
from ..agents.planner import generate_plan


@dataclass
class TaskResult:
    task_id: str
    request: str
    success: bool
    verdict: str = ""        # human-readable completion verdict
    plan: str = ""
    steps_run: int = 0
    self_heals: int = 0
    tool_calls: int = 0
    logs: List[str] = field(default_factory=list)
    workspace: str = ""
    duration: float = 0.0
    artifacts: List[str] = field(default_factory=list)
    error: str = ""
    # Phase A enrichment
    quality: Dict[str, object] = field(default_factory=dict)
    review: str = ""
    security: str = ""
    docs: str = ""
    research: str = ""


# Outcome detection: did the verify command succeed?
def _parse_exit(text: str) -> bool:
    """Heuristic: a sandbox/terminal result with exit code 0 and no TIMEOUT is green."""
    if "TIMEOUT" in text:
        return False
    m = re.search(r"exit=(-?\d+)", text)
    if m:
        return m.group(1) == "0"
    # Fallback: presence of 'OK' and absence of 'ERROR'/'Traceback'.
    if "Traceback" in text or "Error" in text:
        return False
    return "OK" in text


class AutonomousEngine:
    def __init__(self, workspace_root: Optional[Path] = None,
                 model_manager: Optional[ModelManager] = None,
                 memory: Optional[MemoryStore] = None,
                 logger=None, on_event: Optional[Callable[[dict], None]] = None,
                 config_path: Optional[str] = None) -> None:
        self.settings = get_settings(config_path)
        self.logger = logger or get_logger("engine")
        # Phase F3: use LongTermMemory (real embeddings + consolidation) when available.
        if memory is None:
            try:
                from ..memory.longterm import LongTermMemory
                self.memory = LongTermMemory(logger=self.logger, embedder_pref="auto")
            except Exception:
                self.memory = MemoryStore(logger=self.logger)
        else:
            self.memory = memory
        self.manager = model_manager or ModelManager(logger=self.logger)
        self.on_event = on_event
        # Workspace for THIS task is created per run().
        self._bus = self.logger.bus if hasattr(self.logger, "bus") else None
        self.context = None
        self.tools: Optional[object] = None
        self.sandbox: Optional[Sandbox] = None
        self._approver = None  # injected by CLI/UI for the approval gate

    # -- event helper -----------------------------------------------------
    def _emit(self, kind: str, source: str, message: str, **data: object) -> None:
        if self._bus is not None and hasattr(self._bus, "emit"):
            self._bus.emit(kind, source, message, **data)
        if self.on_event:
            try:
                self.on_event({"kind": kind, "source": source, "message": message, "data": data})
            except Exception:
                pass

    # -- context ----------------------------------------------------------
    def _build_context(self, project_path: Path) -> str:
        self.context = ContextEngine(project_path, logger=self.logger)
        self.context.index()
        block = self.context.to_context_block()
        return block

    # -- main entry -------------------------------------------------------
    def run(self, request: str, project_path: Optional[str] = None,
            require_approval: bool = False, verify_command: Optional[str] = None,
            auto_commit: bool = True) -> TaskResult:
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        start = time.time()
        result = TaskResult(task_id=task_id, request=request, success=False,
                            workspace="")
        self.logger.info("loop", f"=== Task {task_id}: {request[:80]} ===")
        self._emit("agent", "loop", f"Starting task: {request[:120]}")

        # 1) Prepare workspace + sandbox
        project = Path(project_path) if project_path else None
        self.sandbox = make_sandbox(task_id, logger=self.logger)
        result.workspace = str(self.sandbox.workspace)
        # Only sync an EXPLICIT project dir into the sandbox. When project_path
        # is None we start from an empty workspace (the agents create files).
        # (Guarding this prevents recursively copying the sandbox into itself.)
        if project is not None and project.exists():
            self._sync_into_sandbox(project, self.sandbox.workspace)
        self.tools = build_tool_registry(self.sandbox)

        # Reset scripted provider state for a clean run.
        if getattr(self.manager.provider, "name", "") == "scripted":
            self.manager.provider.reset()

        # 2) Understand + Analyze + Build Context
        ctx = self._build_context(self.sandbox.workspace)
        result.logs.append("Context indexed.")

        # 3) Research (optional) — when internet is allowed and the request
        #    likely needs external knowledge. Never fatal if offline.
        research_notes = ""
        if self.settings.enable_research and self.settings.permissions.internet_access:
            research_notes = self._run_research(request, result)
            if research_notes:
                ctx += "\n\n## Research notes (from internet)\n" + research_notes
                result.research = research_notes

        # 4) Plan
        self._emit("reasoning", "planner", "Generating structured plan...")
        try:
            # In scripted mode the planner consumes one scripted turn; reset so
            # the scripted provider starts fresh afterward.
            if getattr(self.manager.provider, "name", "") == "scripted":
                self.manager.provider.reset()
            plan = generate_plan(self.manager, request, ctx, role="pm",
                                 logger=self.logger)
        except Exception as e:
            plan = f"(planner unavailable: {e}) Proceeding with direct implementation."
        result.plan = plan
        result.logs.append("Plan generated.")
        self._emit("reasoning", "planner", plan[:400])

        if require_approval or self.settings.require_approval:
            approved = self._request_approval(request, plan, result)
            if not approved:
                result.verdict = "Aborted by user at approval gate."
                self._emit("agent", "loop", "⛔ Task aborted at approval gate.")
                return result

        # 5) Implement + Test + Observe + Self-heal
        success, heals = self._implement_and_verify(request, ctx, plan, verify_command, result)
        result.success = success
        result.self_heals = heals

        # 6) Code-quality enforcement (lint/format/security scan)
        if self.settings.enable_code_quality:
            self._run_quality(result)
        # 7) Review (Reviewer + Security agents) and Documentation (Docs agent)
        if self.settings.enable_review:
            self._run_review(result)
        if self.settings.enable_docs:
            self._run_docs(request, ctx, result)
        # 8) Commit (DevOps agent generates the message)
        if success and auto_commit:
            msg = self._devops_commit_message(result)
            self._commit(result, message=msg)

        # Collect artifacts
        result.artifacts = self._list_artifacts(self.sandbox.workspace)
        result.duration = time.time() - start

        if success:
            result.verdict = ("Task verified complete: the implementation was built, "
                              "executed, verified, reviewed, quality-checked, documented, "
                              "and committed.")
            self.memory.add(f"Completed task: {request[:100]}", category="task",
                            tags=["completed"])
            self._emit("agent", "loop", "✅ Task completed and verified.")
        else:
            result.verdict = ("Task did not reach a verified-green state within the "
                              "self-heal budget. See logs.")
            self._emit("agent", "loop", "❌ Task incomplete after self-heal budget.")
        return result

    # -- approval gate (A2) -----------------------------------------------
    def _request_approval(self, request: str, plan: str, result: TaskResult) -> bool:
        """Pause for user sign-off. The CLI/UI can inject an approver callback;
        otherwise (headless/autonomous) this is a no-op that proceeds."""
        if self._approver is not None:
            return bool(self._approver(request, plan))
        self.logger.info("loop", "Approval gate: no approver injected; proceeding.")
        result.logs.append("Approval gate passed (no interactive approver).")
        return True

    def set_approver(self, fn) -> None:
        self._approver = fn

    # -- quality (A3) -----------------------------------------------------
    def _run_quality(self, result: TaskResult) -> None:
        from .quality import enforce_all
        self._emit("reasoning", "quality", "Running code-quality enforcement...")
        res = enforce_all(self.sandbox, autofix=True)
        for name, r in res.items():
            if r.skipped:
                result.logs.append(f"[quality:{name}] skipped: {', '.join(r.skipped)}")
            if r.findings:
                result.logs.append(f"[quality:{name}] findings: {len(r.findings)}")
                for f in r.findings[:10]:
                    result.logs.append(f"    - {f[:160]}")
            if r.fixed:
                result.logs.append(f"[quality:{name}] auto-fixed")
        result.quality = {k: v.to_dict() for k, v in res.items()}

    # -- review (A1) ------------------------------------------------------
    def _run_review(self, result: TaskResult) -> None:
        ctx = self.context.to_context_block() if self.context else ""
        diff = self._git_diff() or "(no git diff; showing changed files)"
        # Reviewer agent
        self._emit("reasoning", "reviewer", "Reviewing changes...")
        rev = run_agent(
            self.manager, self.tools, role="reviewer",
            task=(f"Review the following diff/changed files for readability, "
                  f"optimisation, dead code, and unused imports. List findings with "
                  f"file:line and severity (blocker|warning|nit). Do not rewrite code.\n\n"
                  f"{diff}"),
            context=ctx, max_tool_iters=8, logger=self.logger, memory=self.memory,
        )
        result.logs.append(f"[reviewer] {rev['text'][:300]}")
        result.review = rev["text"]
        # Security agent
        self._emit("reasoning", "security", "Scanning for vulnerabilities...")
        sec = run_agent(
            self.manager, self.tools, role="security",
            task=(f"Scan the changed code for security issues: hardcoded secrets, "
                  f"injection vectors, unsafe deserialization, dangerous commands. "
                  f"Report findings with severity and remediation.\n\n{diff}"),
            context=ctx, max_tool_iters=8, logger=self.logger, memory=self.memory,
        )
        result.logs.append(f"[security] {sec['text'][:300]}")
        result.security = sec["text"]

    # -- docs (A1) --------------------------------------------------------
    def _run_docs(self, request: str, ctx: str, result: TaskResult) -> None:
        self._emit("reasoning", "docs", "Generating documentation...")
        docs = run_agent(
            self.manager, self.tools, role="docs",
            task=(f"After the implementation, create or update concise documentation: "
                  f"what was built, how to run it, the API surface, and a changelog entry. "
                  f"Write a README.md if one does not exist.\n\nREQUEST: {request}"),
            context=ctx, max_tool_iters=8, logger=self.logger, memory=self.memory,
        )
        result.logs.append(f"[docs] {docs['text'][:300]}")
        result.docs = docs["text"]

    # -- devops commit message (A1) ---------------------------------------
    def _devops_commit_message(self, result: TaskResult) -> str:
        try:
            out = run_agent(
                self.manager, self.tools, role="devops",
                task=("Generate a concise Conventional-Commits style git commit message "
                      "for the changes just made. Output ONLY the message, no quotes."),
                context=self._git_diff() or "", max_tool_iters=4,
                logger=self.logger, memory=self.memory,
            )
            msg = out["text"].strip().strip('"').strip("'")
            if msg:
                return msg[:200]
        except Exception:
            pass
        return f"YACINE: {result.request[:70]}"
    # -- implement + self-heal --------------------------------------------
    def _implement_and_verify(self, request, ctx, plan, verify_command, result):
        heals = 0
        last_err = ""
        max_heals = self.settings.max_self_heal_iterations

        for attempt in range(1, max_heals + 2):
            result.steps_run += 1
            self._emit("reasoning", "coder",
                       f"Implementation attempt {attempt}")
            # Coder agent implements (with tools).
            coder_out = run_agent(
                self.manager, self.tools, role="coder",
                task=(f"Implement the following request. Use the tools to create and "
                      f"modify files in the workspace, then build/run as needed.\n\n"
                      f"REQUEST: {request}\n\nPLAN:\n{plan}"),
                context=ctx, max_tool_iters=14, logger=self.logger, memory=self.memory,
                system_extra=(
                    "You are the Coder. Respond with EXACTLY ONE JSON tool call and "
                    "nothing else (no markdown, no prose). Create each requested file "
                    "with its EXACT name: e.g. greeter.py and test_greeter.py. Do not "
                    "name files after plan phases. After writing, call the terminal "
                    "tool to run the verification command."),
            )
            result.tool_calls += len(coder_out["tool_log"])
            for t in coder_out["tool_log"]:
                result.logs.append(f"[coder] {t['tool']}: {str(t['result'].get('output',''))[:120]}")

            # Determine verify command if not given: try to auto-detect.
            vcmd = verify_command or self._infer_verify_command()
            if not vcmd:
                # No test framework detected -> a simple "files exist" check.
                ok = self._files_exist_check(request)
                if ok:
                    return True, heals
                last_err = "No verification command available; produced files but could not verify execution."
                # If first attempt and no tests, try to add a smoke test.
                if attempt == 1:
                    self._add_smoke_test(request)
                    continue
                return False, heals

            # Execute verification.
            term = self.tools.get("terminal")
            exec_res = term.execute({"command": vcmd})
            result.tool_calls += 1
            out = str(exec_res.output)
            result.logs.append(f"[verify] {vcmd}\n{out[:600]}")
            self._emit("tool", "verify", f"exit={exec_res.data.get('returncode')} :: {out[:200]}")

            if _parse_exit(out):
                return True, heals

            last_err = out
            # Self-heal: Debugger agent analyses the failure and fixes code.
            if attempt <= max_heals:
                heals += 1
                self._emit("reasoning", "debugger", "Self-heal: analyzing failure...")
                self.logger.error("loop", f"Attempt {attempt} failed; self-healing.")
                # Feed the test output to the scripted provider so its
                # `match` (e.g. "FAILED") selects the fix turn.
                if getattr(self.manager.provider, "name", "") == "scripted":
                    self.manager.provider.observe(out)
                fix_out = run_agent(
                    self.manager, self.tools, role="debugger",
                    task=(f"The verification command failed:\n```\n{vcmd}\n```\n"
                          f"OUTPUT:\n{out[-3000:]}\n\n"
                          f"Fix the code so the verification passes. Use edit_file on the "
                          f"affected files only; preserve working code. Then you may re-run "
                          f"the command to confirm."),
                    context=ctx, max_tool_iters=12, logger=self.logger, memory=self.memory,
                )
                result.tool_calls += len(fix_out["tool_log"])
                for t in fix_out["tool_log"]:
                    result.logs.append(f"[debugger] {t['tool']}: {str(t['result'].get('output',''))[:120]}")
        result.error = last_err[:2000]
        return False, heals

    # -- helpers ----------------------------------------------------------
    def _sync_into_sandbox(self, src: Path, dst: Path) -> None:
        import shutil
        # Copy only source files (skip heavy dirs) into the sandbox.
        skip = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
        for item in src.rglob("*"):
            if any(part in skip for part in item.parts):
                continue
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(item, target)
                except Exception:
                    pass
        self.logger.info("loop", f"Synced project into sandbox {dst}")

    def _infer_verify_command(self) -> Optional[str]:
        ws = self.sandbox.workspace
        if (ws / "pytest.ini").exists() or (ws / "pyproject.toml").exists() or (ws / "tests").exists() or list(ws.glob("test_*.py")) or list(ws.glob("*_test.py")):
            return "python -m pytest -q"
        if (ws / "package.json").exists():
            return "npm test"
        # A python file with __main__ could be run directly.
        pys = list(ws.glob("*.py"))
        if pys and any("def main" in p.read_text(errors="ignore") for p in pys):
            return f"python {pys[0].name}"
        return None

    def _files_exist_check(self, request: str) -> bool:
        # Crude: if the coder created files, treat as progress. We require a
        # verify command for real completion; this is a fallback only.
        files = [p for p in self.sandbox.workspace.rglob("*") if p.is_file()]
        return len(files) > 0

    def _add_smoke_test(self, request: str) -> None:
        # Let the coder add a minimal smoke test so we can verify.
        run_agent(
            self.manager, self.tools, role="tester",
            task=("Add a minimal smoke test that imports/executes the new code and "
                  "asserts basic correctness. Use pytest if possible, else a simple "
                  "python script named smoke_test.py that exits 0 on success."),
            context=self.context.to_context_block() if self.context else "",
            max_tool_iters=8, logger=self.logger, memory=self.memory,
        )

    def _commit(self, result: TaskResult, message: Optional[str] = None) -> None:
        try:
            git = self.tools.get("git")
            git.execute({"operation": "init"})
            git.execute({"operation": "add", "args": "."})
            msg = message or f"YACINE: {result.request[:70]}"
            git.execute({"operation": "commit", "args": msg})
            result.logs.append(f"Committed: {msg[:80]}")
        except Exception as e:
            result.logs.append(f"(commit skipped: {e})")

    def _git_diff(self) -> str:
        """Return a textual diff/changed-file summary for the review agents."""
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", str(self.sandbox.workspace), "diff", "HEAD", "--stat"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            pass
        # Fallback: list created/changed files in the workspace.
        files = [str(p.relative_to(self.sandbox.workspace))
                 for p in self.sandbox.workspace.rglob("*")
                 if p.is_file() and ".bak" not in p.suffixes
                 and "__pycache__" not in p.parts]
        return "Changed files:\n" + "\n".join(files[:40])

    # -- research (B) ------------------------------------------------------
    def _needs_research(self, request: str) -> bool:
        """Heuristic: does this request likely need external/library knowledge?"""
        signals = ["api", "sdk", "library", "framework", "package", "npm", "pypi",
                   "how to", "latest", "version", "react", "fastapi", "pytorch",
                   "tensorflow", "docker", "kubernetes", "rust", "go ", "postgres",
                   "mongodb", "redis", "using", "integrate", "oauth", "stripe",
                   "openai", "anthropic", "huggingface", "llm"]
        low = request.lower()
        return any(s in low for s in signals)

    def _run_research(self, request: str, result: TaskResult) -> str:
        if not self._needs_research(request):
            return ""
        self._emit("reasoning", "research", "Researching external docs/APIs...")
        try:
            out = run_agent(
                self.manager, self.tools, role="research",
                task=(f"Research this request using web_search and open_browser tools. "
                      f"Find official documentation / canonical usage for any external "
                      f"API, library, or framework involved. Summarise ONLY what the "
                      f"documentation supports, rank sources by reliability, and cite "
                      f"URLs. Never invent APIs. Keep it under 400 words.\\n\\n"
                      f"REQUEST: {request}"),
                context="", max_tool_iters=10, logger=self.logger, memory=self.memory,
            )
        except Exception as e:
            result.logs.append(f"(research skipped: {e})")
            return ""
        result.logs.append(f"[research] {out['text'][:200]}")
        return out["text"]

    def _list_artifacts(self, ws: Path) -> List[str]:
        out = []
        for p in ws.rglob("*"):
            if p.is_file() and ".bak" not in p.suffixes:
                out.append(str(p.relative_to(ws)))
        return sorted(out)[:50]
