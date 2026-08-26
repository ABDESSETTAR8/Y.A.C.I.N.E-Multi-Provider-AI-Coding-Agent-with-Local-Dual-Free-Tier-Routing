"""Agent: a persona-bound model runner with tool-calling.

An Agent = a specialized persona (system prompt) + the shared model manager +
the tool registry. It takes a task, optionally performs multiple rounds of
tool calls (agentic loop), and returns the final answer. This is the unit the
orchestrator composes into the full autonomous development lifecycle.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..models.types import Message, ModelResponse
from ..models.manager import ModelManager
from ..tools.base import ToolRegistry
from .personas import PERSONAS


def _strip_quotes(s: str) -> str:
    """Remove a single layer of matching surrounding quotes (incl. triple)."""
    s = s.strip()
    for q in ('"""', "'''", '"', "'"):
        if len(s) >= 2 * len(q) and s.startswith(q) and s.endswith(q):
            return s[len(q):-len(q)]
    return s


def _split_top_level(s: str) -> List[str]:
    """Split on top-level commas, respecting quotes, brackets, and parens."""
    out, buf, depth = [], [], 0
    in_str, qc = False, ""
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            buf.append(c)
            if c == qc and (i + 1 >= len(s) or s[i + 1] != qc):
                in_str = False
            elif c == qc:
                # doubled quote escape inside string
                pass
            i += 1
            continue
        if c in ('"', "'"):
            in_str, qc = True, c
            buf.append(c)
        elif c in "([{":
            depth += 1
            buf.append(c)
        elif c in ")]}":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return [x for x in out if x != ""]


class Agent:
    def __init__(self, manager: ModelManager, tools: ToolRegistry,
                 role: str = "coder", logger=None, memory=None) -> None:
        self.manager = manager
        self.tools = tools
        self.role = role
        self.logger = logger
        self.memory = memory
        self.system = PERSONAS.get(role, PERSONAS["coder"])

    def _tool_schemas(self) -> Optional[List[dict]]:
        # Only pass tools if the provider supports calling them.
        if getattr(self.manager.provider, "supports_tools", False):
            return self.tools.schemas()
        return None

    def _format_tool_result(self, name: str, result) -> str:
        return f"TOOL RESULT [{name}]:\n{str(result)}"

    def _recover_code_blocks_as_files(self, text: str, task: str):
        """Last-resort recovery: if the model emitted code in markdown fences
        instead of calling write_file, extract those blocks and actually write
        them. Returns a list of (tool_name, args, ToolResult) tuples.

        Filename is taken from a leading `# file: x.py` / `// file: x.py` comment
        when present, else derived from the task (e.g. 'greeter' -> greeter.py)."""
        import re
        if "```" not in text:
            return []
        blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.DOTALL)
        if not blocks:
            return []
        # Build a guess for the filename from the task.
        task_lc = task.lower()
        guess = None
        for tok in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", task_lc):
            if tok in ("create", "python", "module", "file", "test", "with", "a", "the", "function", "using", "ensure", "plus"):
                continue
            guess = tok
            break
        recovered = []
        for i, code in enumerate(blocks):
            code = code.rstrip("\n")
            # Try to find an explicit filename in the first comment line.
            m = re.search(r"(?:#|//|<!--)\s*file:\s*([^\s]+)", code)
            fname = m.group(1).strip() if m else None
            if not fname:
                # guess extension from content
                ext = ".py" if ("def " in code or "import " in code) else ".txt"
                base = guess or f"generated_{i}"
                fname = f"{base}{ext}"
            args = {"path": fname, "content": code}
            result = self.tools.call("write_file", args)
            if self.logger:
                self.logger.tool(self.role, f"→ (auto) write_file({fname})")
            recovered.append(("write_file", args, result))
        return recovered

    def _parse_json_tool_calls(self, text: str):
        """Many local code models (e.g. Qwen2.5-Coder via Ollama) do NOT emit
        native tool_calls; they return a JSON tool-call object (or a list of
        them) inside the message content. Parse that into the same
        {name, arguments} shape the loop expects.

        Accepts:
          - a bare JSON object: {"name": "...", "arguments": {...}}
          - a list of such objects
          - a JSON object wrapped in ```json fences or surrounded by prose.
        Returns [] if nothing parseable is found.
        """
        import json
        import re
        if not text or not text.strip():
            return []
        candidates = []
        # 1) fenced code block (```json ... ```)
        for m in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
            candidates.append(m.strip())
        # 2) any {...} or [...] object in the text
        for m in re.findall(r"(\{.*\}|\[.*\])", text, re.DOTALL):
            candidates.append(m.strip())
        # 3) the whole thing as-is
        candidates.append(text.strip())
        calls = []
        for cand in candidates:
            try:
                obj = json.loads(cand)
            except Exception:
                continue
            items = obj if isinstance(obj, list) else [obj]
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = it.get("name") or it.get("function", {}).get("name") or ""
                if not name:
                    continue
                args = it.get("arguments") or it.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                calls.append({"name": name, "arguments": args or {}})
            if calls:
                break
        return calls

    def _parse_python_tool_calls(self, text: str):
        """Parse Python-style tool invocations the model sometimes emits in
        prose instead of JSON, e.g.:

            write_file('workspace/webapp/index.html', '<!DOCTYPE html>...')

        Handles single/double quotes, balanced parentheses, and embedded
        escaped quotes. Also matches ``tool("name", {...})`` with a JSON-ish
        second argument. Returns a list of {name, arguments} dicts."""
        import re
        if not text:
            return []
        calls = []
        # Match: identifier( first_arg , second_arg )  with balanced parens.
        # We capture the name and the full inner text, then split arguments.
        pat = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(((?:[^()\"']|\"[^\"]*\"|'[^']*'|\([^()]*\))*)\)")
        known = set(self.tools.names()) if self.tools else set()
        for m in pat.finditer(text):
            name = m.group(1)
            # Only treat recognised tools as calls (ignore def/print/etc.).
            if name not in known:
                continue
            inner = m.group(2).strip()
            if not inner:
                continue
            # Split top-level args on commas (respecting quotes/brackets).
            args_raw = _split_top_level(inner)
            args = {}
            if len(args_raw) == 1:
                # Single argument: treat as the content/value (legacy form).
                args = {"content": _strip_quotes(args_raw[0])}
            elif len(args_raw) >= 2:
                # (path, content) or (path, content, extra)
                args = {"path": _strip_quotes(args_raw[0]),
                        "content": _strip_quotes(args_raw[1])}
                if len(args_raw) > 2:
                    args["extra"] = _strip_quotes(args_raw[2])
            # Normalise: if the second arg looks like JSON, parse it.
            if len(args_raw) >= 2 and args_raw[1].strip().startswith("{"):
                try:
                    args["arguments"] = json.loads(args_raw[1])
                except Exception:
                    pass
            calls.append({"name": name, "arguments": args})
        return calls

    def _extract_calls(self, resp):
        """Union of native tool_calls, JSON-in-text, and Python-style calls."""
        native = list(resp.tool_calls or [])
        if native:
            return native
        text = resp.text or ""
        calls = self._parse_json_tool_calls(text)
        if calls:
            return calls
        return self._parse_python_tool_calls(text)

    def run(self, task: str, context: str = "", max_tool_iters: int = 12,
            system_extra: str = "") -> Dict[str, object]:
        """Execute the agent on `task`. Returns dict with text, transcript, tool_log."""
        system = self.system
        if context:
            system += f"\n\nPROJECT CONTEXT:\n{context}"
        if system_extra:
            system += f"\n\n{system_extra}"
        # Inject relevant memory as a soft prefix in the first user turn.
        memory_hint = ""
        if self.memory:
            mem = self.memory.recall(task, limit=3)
            if mem and "no relevant" not in mem:
                memory_hint = f"\n\nRELEVANT MEMORY:\n{mem}"

        messages: List[Message] = [Message(role="system", content=system)]
        user_msg = task + memory_hint
        messages.append(Message(role="user", content=user_msg))

        tool_log: List[Dict[str, object]] = []
        final_text = ""
        iterations = 0

        # For the scripted provider (offline/tests) we run a single deterministic
        # turn: the script emits all tool_calls at once, we execute them, and stop.
        # This keeps scripted sequencing predictable (one complete() per agent call).
        single_shot = getattr(self.manager.provider, "name", "") == "scripted"

        if single_shot:
            iterations = 1
            resp = self.manager.complete(messages, tools=self._tool_schemas())
            final_text = resp.text
            calls = self._extract_calls(resp)
            if calls:
                messages.append(Message(role="assistant", content=resp.text or ""))
                for call in calls:
                    name = call.get("name", "")
                    args = call.get("arguments", {}) or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if self.logger:
                        self.logger.tool(self.role, f"→ {name}({json.dumps(args)[:200]})")
                    result = self.tools.call(name, args)
                    if self.logger:
                        self.logger.tool(self.role, f"← {name}: {(str(result.output)[:160])}")
                    tool_log.append({"tool": name, "args": args, "result": result.to_dict()})
                    messages.append(Message(role="tool",
                                           content=self._format_tool_result(name, result),
                                           name=name, tool_call_id=call.get("id")))
            return {
                "text": final_text,
                "iterations": iterations,
                "tool_log": tool_log,
                "messages": [m.to_dict() for m in messages],
            }

        while iterations < max_tool_iters:
            iterations += 1
            resp: ModelResponse = self.manager.complete(messages, tools=self._tool_schemas())
            final_text = resp.text

            calls = self._extract_calls(resp)
            if not calls:
                # Layer 3: if the model dropped a code block in prose instead of
                # calling write_file, extract it and write the file for it. This
                # keeps Qwen-style models productive even when they ignore the
                # tool schema.
                extracted = self._recover_code_blocks_as_files(resp.text, task)
                if extracted:
                    for name, args, result in extracted:
                        rec = {"tool": name, "args": args,
                               "result": {"ok": getattr(result, "ok", False),
                                          "output": str(getattr(result, "output", result))}}
                        tool_log.append(rec)
                        messages.append(Message(
                            role="tool",
                            content=self._format_tool_result(name, result),
                            name=name, tool_call_id=f"auto_{len(tool_log)}"))
                    # Re-prompt the model to continue (run tests, etc.).
                    messages.append(Message(
                        role="user",
                        content="Files were created via tools. Now run the build/test "
                                "command with the terminal tool and report results."))
                    continue
                # No tool calls and nothing recoverable -> agent is done reasoning.
                break

            # Append the assistant message (with tool-call intent).
            messages.append(Message(role="assistant", content=resp.text or ""))

            for call in calls:
                name = call.get("name", "")
                args = call.get("arguments", {}) or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if self.logger:
                    self.logger.tool(self.role, f"→ {name}({json.dumps(args)[:200]})")
                result = self.tools.call(name, args)
                if self.logger:
                    self.logger.tool(self.role, f"← {name}: {(str(result.output)[:160])}")
                tool_log.append({"tool": name, "args": args, "result": result.to_dict()})
                # Feed the result back to the model.
                messages.append(Message(
                    role="tool",
                    content=self._format_tool_result(name, result),
                    name=name,
                    tool_call_id=call.get("id"),
                ))

        return {
            "text": final_text,
            "iterations": iterations,
            "tool_log": tool_log,
            "messages": [m.to_dict() for m in messages],
        }


def run_agent(manager, tools, role, task, context="", max_tool_iters=12,
              logger=None, memory=None, system_extra="") -> Dict[str, object]:
    agent = Agent(manager, tools, role=role, logger=logger, memory=memory)
    return agent.run(task, context=context, max_tool_iters=max_tool_iters,
                     system_extra=system_extra)
