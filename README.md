# Y.A.C.I.N.E

**Y**et **A**nother **C**ognitive **I**ntelligent **N**eural **E**ngineer

A local, privacy-first, **autonomous** software-engineering agent for Windows 11
(RTX 2080 Super, 32 GB RAM). It behaves like a senior engineer — it thinks,
plans, implements, executes, observes, self-debugs, retries, validates, and
**only then** delivers verified, production-ready code. It never stops after
generating code.

> This repository is the working implementation built from `YACINE CODER AGENT.pdf`.
> It is a real, runnable framework. The model backend is pluggable; the system
> currently ships with a **scripted provider** so the whole autonomous loop runs
> and is **tested end-to-end with no GPU/model installed**, plus ready-to-use
> **Ollama** (Qwen2.5-Coder) and **OpenAI-compatible** (LM Studio / vLLM) drivers.

---

## Status (what is implemented today)

| Spec area | Status |
|---|---|
| Modular architecture (Core / Models / Agents / Tools / Memory / API / UI / Config) | ✅ |
| Interchangeable model providers (Ollama, OpenAI-compat, scripted) | ✅ |
| Multi-agent system (PM, Architect, Research, Coder, Reviewer, Debugger, Tester, Security, DevOps, Docs) | ✅ orchestrated end-to-end |
| Autonomous development loop (understand → research → plan → implement → test → self-heal → quality → review → security → docs → commit → deliver) | ✅ full pipeline |
| Planning engine (requirements, architecture, risks, edge cases, perf, security, files, steps, testing, rollback) | ✅ |
| Context engine (repo indexing, symbols, imports, tree, search, git-history, coding-style detection) | ✅ |
| Persistent memory with embeddings + vector/keyword search | ✅ (LongTermMemory: real ST embeddings + consolidation when installed) |
| Tool system (filesystem, code-edit, terminal, Python, search, HTTP, web-search, browser, git, **DB**, **MCP**, **vision**, **voice**) | ✅ |
| Execution sandbox (isolated workspace, timeout, mem cap, stdout/stderr/return-code capture) | ✅ |
| Self-healing engine (capture traceback → root-cause → fix only affected code → retry → green) | ✅ |
| Code-quality enforcement (black/ruff/eslint/prettier + py_compile, config-gated) | ✅ |
| Coverage + regression/stress test generation | ✅ |
| Testing system (auto-generate + run + repair) | ✅ (driven by agents) |
| Internet research agent (cited, ranked, no-hallucination) | ✅ |
| Database support (SQLite/Postgres/MySQL/Mongo + schema discovery + migrations) | ✅ (SQLite built-in) |
| MCP client (expose MCP server tools in registry) | ✅ |
| Plugin SDK (loader: tools/agents/providers/UI/commands) | ✅ |
| Security / permissions (privacy-first, destructive-command block, git_push off by default) | ✅ |
| REST + WebSocket API | ✅ |
| Desktop GUI (Claude/Cursor-like: chat, explorer, terminal, GPU/CPU/RAM/disk monitors, markdown+mermaid, upload, search/export, dark/light) | ✅ `/ui` |
| Voice (TTS/STT local, graceful fallback) | ✅ (offline-safe) |
| Vision (image/PDF upload multimodal hook, graceful fallback) | ✅ (offline-safe) |
| CLI with Rich UI | ✅ |

---

## Quick start (offline, no model needed)

```powershell
cd C:\Users\MOH\Desktop\Y.A.C.I.N.E
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py doctor        # environment sanity check
python -m pytest -q          # proves the autonomous loop completes + self-heals
python main.py run "Create a calc module with add and mul and unit tests" --verify "python -m pytest -q"
```

The `run` above uses the **scripted provider** (configured in `config/settings.yaml`),
so it executes the full autonomous lifecycle deterministically and ends green —
demonstrating the self-healing loop without a model.

---

## Run it LIVE with a local model (Qwen2.5-Coder via Ollama)

This is the intended "real" mode from the spec. Ollama runs 100% locally.

### 1. Install Ollama
Download from https://ollama.com/download (Windows). Verify:
```powershell
ollama --version
```

### 2. Pull Qwen2.5-Coder (7B fits the 8 GB VRAM of the RTX 2080 Super; use the 3B/1.5B for more headroom)
```powershell
ollama pull qwen2.5-coder:7b
# or, lighter:  ollama pull qwen2.5-coder:3b
```

### 3. Point Y.A.C.I.N.E at Ollama
Edit `config/settings.yaml`:
```yaml
model:
  provider: ollama
  model: qwen2.5-coder:7b
  base_url: "http://localhost:11434"
```
(Or set env vars: `YACINE_MODEL_PROVIDER=ollama`, `YACINE_MODEL_NAME=qwen2.5-coder:7b`.)

### 4. Run a real autonomous task
```powershell
python main.py serve            # start REST + WebSocket API on :8765
# in another terminal:
python main.py run "Build a FastAPI endpoint /items that returns a JSON list" --project ./myapp --verify "pytest -q"
python main.py chat --role coder   # interactive agent (tool-enabled)
```

> **LM Studio / vLLM alternative:** set `provider: openai` and
> `base_url: http://localhost:1234/v1` (LM Studio) or your vLLM endpoint.
> Any OpenAI-compatible server works unchanged — providers are switchable
> without touching code.

---

## How the autonomous loop works

```
request
  │
  ▼
Understand → Analyze Project → Build Context (repo index + memory)
  │
  ▼
Research (web/docs) *if needed, never fatal offline*
  │
  ▼
Generate Plan (PM persona: requirements, architecture, steps, risks…)
  │
  ▼
Implement (Coder agent calls tools: write_file / edit_file / terminal)
  │
  ▼
Generate Tests + Execute verify_command (pytest / npm test / custom)
  │
  ├─ green? ──► Review → Optimize → Docs → Commit → DELIVER ✅
  │
  └─ red? ───► Debugger agent reads traceback, fixes ONLY affected code,
                preserves working code, re-runs  ──► (repeat up to N times)
```

The engine records every reasoning step, tool call, error, and fix to logs and
a live event bus (consumed by the WebSocket API and future Desktop UI).

---

## Architecture

```
yacine/
  core/        config, permissions, logger/event-bus, security, context engine, engine (orchestrator)
  models/      provider abstraction: ollama, openai_compat, scripted  (+ types, manager)
  agents/      personas (10 specialized roles), planner, agent runner
  tools/       registry, sandbox, filesystem, code_edit, terminal, internet, browser, git
  memory/      persistent store + hashed embedding vector/keyword search
  api/         FastAPI REST + WebSocket server
  cli.py       Rich CLI (chat / run / serve / memory / context / config / models / doctor)
main.py        console entry point
tests/         pytest suite incl. full-loop self-heal proof + fixtures
config/        settings.yaml
```

### Key design choices
- **Provider-agnostic:** the engine only talks to `ModelManager.complete()`. Swap
  Ollama ↔ LM Studio ↔ scripted via config. No code changes.
- **Sandboxed execution:** every command runs in an isolated per-task workspace
  with a timeout and memory ceiling; stdout/stderr/return-code are captured and
  parsed to decide green/red.
- **Privacy-first security:** all sensitive ops are permission-gated; destructive
  shell patterns are blocked; `git_push` is OFF by default.
- **Offline-capable:** the `scripted` provider + hashed embeddings mean the
  entire framework (and its tests) run with zero network and zero GPU.

---

## CLI reference

| Command | Purpose |
|---|---|
| `python main.py doctor` | environment sanity check (python/ollama/node/git/model) |
| `python main.py models` | check which providers are reachable |
| `python main.py chat --role coder` | interactive agent (10 roles available) |
| `python main.py run "<request>" [--project DIR] [--verify CMD] [--no-commit]` | full autonomous task |
| `python main.py serve [--host 127.0.0.1 --port 8765]` | REST + WebSocket API |
| `python main.py context <path>` | index + summarise a project |
| `python main.py memory list|add|search|forget` | persistent memory |
| `python main.py config --show` | view/edit config (model, permissions) |

### API
- `GET  /health`
- `POST /run`  `{request, project_path?, verify_command?, auto_commit?}` → `TaskResult`
- `POST /run/stream` → SSE event stream ending with `result`
- `WS   /ws` → live reasoning/tool/agent/error events
- `GET/POST /memory`, `GET /context/summary`

Optional auth: set `YACINE_API_KEY` and send `X-API-Key`.

---

## Testing

The suite (`tests/test_engine.py`) is the proof-of-life. It runs the **real**
engine with the scripted provider and asserts the full loop reaches a verified
green state after self-healing from a deliberate bug — plus sandbox capture,
context indexing/search, memory persistence/similarity, tool registry, and the
REST API.

```powershell
python -m pytest -q
```

---

## Roadmap (per the spec's phases)

- **Phase 1–3 (this repo):** core arch, model integration, multi-agent
  orchestration, autonomous loop, sandbox, self-heal, indexing, memory, git.
- **Phase 4:** live internet research agent, documentation retrieval, MCP servers, plugin SDK.
- **Phase 5:** Desktop GUI (Claude/Cursor-like: dark/light, resizable panels,
  tabbed chats, project explorer, terminal, execution monitor, token/GPU/CPU/RAM
  usage, streaming, artifacts, Mermaid/markdown).
- **Phase 6:** voice + vision, long-term memory improvements, collaborative
  multi-agent execution, Docker sandboxes.

Each phase is additive — the engine, providers, tools, and memory already exist
and are tested, so new capabilities compose without rewrites.

---

## Requirements

- Windows 11 (also runs on Linux/macOS)
- Python 3.11+, `pip install -r requirements.txt`
- Optional: Ollama (+ `qwen2.5-coder`), or any OpenAI-compatible server (LM Studio, vLLM)
- Optional: Node.js (for JS/TS tasks), Git

Built to evolve into a complete autonomous AI development platform — modular,
extensible, privacy-first, and performant on consumer hardware.
