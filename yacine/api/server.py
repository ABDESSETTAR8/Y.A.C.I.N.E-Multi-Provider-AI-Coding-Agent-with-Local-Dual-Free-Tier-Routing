"""FastAPI REST + WebSocket server for Y.A.C.I.N.E.

Exposes:
  GET  /health                 -> provider/model status
  GET  /context/summary        -> repo context summary
  POST /chat                   -> single agent turn (agent role + message)
  POST /run                    -> full autonomous task (returns TaskResult)
  POST /run/stream             -> autonomous task with SSE event stream
  GET  /memory                 -> list memories
  POST /memory                 -> add a memory
  WS   /ws                     -> live event stream (reasoning/tool/agent/error)

Auth: optional API key via X-API-Key header (set YACINE_API_KEY).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..core.config import get_settings
from ..core.auth import get_api_key, key_source
from ..core.logger import EventBus, get_bus, get_logger
from ..core.engine import AutonomousEngine, TaskResult
from ..models.manager import ModelManager, MultiProviderManager
from ..orchestrator import MultiAgentOrchestrator
from ..memory.store import MemoryStore
from ..agents.personas import PERSONAS
from ..ui.backend import router as ui_router, monitor_endpoint

_settings = get_settings()
_logger = get_logger("api")
_bus: EventBus = get_bus()
_memory = MemoryStore(logger=_logger)
_manager = MultiProviderManager(logger=_logger)

# --------------------------------------------------------------------------
# Auth.
#
# This server drives a shell on the user's machine, so it is NOT safe to leave
# open. The key comes from YACINE_API_KEY; when that is unset we generate one
# per process and print it, rather than silently running with no auth. The
# provider's model API key is never reused for this — they are different
# secrets with different audiences.
# --------------------------------------------------------------------------
API_KEY = get_api_key(Path(_settings.data_dir))

# Browsers must not be able to reach this server from arbitrary pages: a
# wildcard origin turns every site the user visits into a remote shell.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "YACINE_ALLOWED_ORIGINS",
        "http://127.0.0.1:8765,http://localhost:8765",
    ).split(",") if o.strip()
]
# Only these Host values are served, which blocks DNS-rebinding attacks that
# resolve an attacker-controlled name to 127.0.0.1.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        "YACINE_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]",
    ).split(",") if h.strip()
]

app = FastAPI(title="Y.A.C.I.N.E API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
    allow_methods=["GET", "POST"], allow_headers=["X-API-Key", "Content-Type"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.include_router(ui_router)

# Paths served without a key: the UI shell itself and its static assets, which
# carry no data. Everything else authenticates.
_PUBLIC_PATHS = ("/ui", "/docs", "/redoc", "/openapi.json", "/favicon.ico")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Authenticate every route by default.

    Previously each handler opted in by calling _check_auth(), and several
    (/health, /models, /context/summary, /memory) never did. Default-deny
    removes that whole class of oversight.
    """
    path = request.url.path
    if request.method == "OPTIONS" or path.startswith(_PUBLIC_PATHS):
        return await call_next(request)
    key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(key, API_KEY):
        return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)


@app.get("/api/monitor")
def monitor():
    return monitor_endpoint()


def _check_auth(req: Request) -> None:
    """Kept for compatibility; the middleware above is the real gate."""
    return None


def _check_ws_auth(ws: WebSocket) -> bool:
    """WebSockets bypass HTTP middleware, so they are checked explicitly.

    The key may arrive as a header or, because browser WebSocket clients
    cannot set headers, as an `?api_key=` query parameter.
    """
    key = ws.headers.get("X-API-Key", "") or ws.query_params.get("api_key", "")
    return secrets.compare_digest(key, API_KEY)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    role: str = "chat"
    context: str = ""
    model: Optional[str] = None   # "provider:model" to switch before replying


class RunRequest(BaseModel):
    request: str
    project_path: Optional[str] = None
    verify_command: Optional[str] = None
    require_approval: bool = False
    auto_commit: bool = True
    stream: bool = True


class ModelSwitchRequest(BaseModel):
    provider: str = "ollama"
    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    if isinstance(_manager, MultiProviderManager):
        active = _manager.active
        return {
            "status": "ok",
            "model_provider": active.cfg.provider,
            "model": active.model_name,
            "model_available": active.available(),
            "multi_provider": True,
            "role_map": _manager._role_map,
            "agents": list(PERSONAS.keys()),
            "pipeline": list(_manager._role_map.keys())[:4],  # architect, orchestrator, etc.
            "auth_required": bool(API_KEY),
        }
    return {
        "status": "ok",
        "model_provider": _manager.cfg.provider,
        "model": _manager.model_name,
        "model_available": _manager.available(),
        "agents": list(PERSONAS.keys()),
        "pipeline": _orchestrator_models(),
        "auth_required": bool(API_KEY),
    }


@app.get("/models")
def models():
    """Active model + available models for selection in the UI.
    
    When using MultiProviderManager, lists all role→model mappings so the
    UI can show which provider each pipeline stage uses.
    """
    from ..models.manager import MultiProviderManager
    if isinstance(_manager, MultiProviderManager):
        active = _manager.active
        return {
            "active": {"provider": active.cfg.provider, "model": active.model_name},
            "available": [],
            "providers": ["ollama", "openai_compatible"],
            "multi": True,
            "role_map": _manager._role_map,
        }
    available = []
    # Ollama models (if reachable).
    try:
        import httpx
        r = httpx.get(f"{_manager.cfg.base_url}/api/tags", timeout=5)
        if r.status_code == 200:
            for m in r.json().get("models", []):
                available.append({"provider": "ollama", "model": m["name"],
                                  "size": m.get("size", 0)})
    except Exception:
        pass
    # Always offer the configured providers.
    providers = ["ollama", "openai", "scripted"]
    return {
        "active": {"provider": _manager.cfg.provider, "model": _manager.model_name},
        "available": available,
        "providers": providers,
    }


@app.post("/chat/model")
def chat_set_model(req: ModelSwitchRequest, request: Request):
    """Switch the active model.
    
    When using MultiProviderManager, the 'model' field is treated as the
    model name for the 'chat' role. The role→model mapping is used to
    determine which provider handles it (poolside/* → ollama, else gemini).
    """
    _check_auth(request)
    overrides = {}
    if req.base_url:
        overrides["base_url"] = req.base_url
    if req.api_key:
        overrides["api_key"] = req.api_key
    try:
        if isinstance(_manager, MultiProviderManager):
            # Map role → model name
            _manager.set_role_model("chat", req.model)
        else:
            _manager.set_model(req.provider, req.model, **overrides)
    except Exception as e:
        raise HTTPException(500, f"model switch failed: {e}")
    # Persist so the change survives a restart.
    try:
        from ..core.config import save_config
        s = get_settings()
        if isinstance(_manager, MultiProviderManager):
            # Update role_map in settings
            if not hasattr(s, "model_role_map") or s.model_role_map is None:
                s.model_role_map = {}
            s.model_role_map["chat"] = req.model
        else:
            s.model.provider = req.provider
            s.model.model = req.model
        if req.base_url:
            s.model.base_url = req.base_url
        if req.api_key:
            s.model.api_key = req.api_key
        save_config(s)
    except Exception:
        pass
    if isinstance(_manager, MultiProviderManager):
        active = _manager.active
        return {"active": {"provider": active.cfg.provider, "model": active.model_name}}
    return {"active": {"provider": _manager.cfg.provider, "model": _manager.model_name}}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Streaming chat: emits SSE tokens as the model thinks (live thinking)."""
    _check_auth(request)
    from sse_starlette.sse import EventSourceResponse
    from ..agents.personas import PERSONAS
    from ..models.types import Message

    # Chat uses the FAST, warm model (3B) by default so replies are snappy and
    # the model stays resident between turns. A one-shot model switch (incl.
    # switching back to the heavy coder model) is still allowed per-request.
    chat_cfg = _settings.chat_model
    # set_model is a no-op when this is already the active configuration, so
    # repeated chat turns no longer rebuild the provider or churn the model.
    try:
        if isinstance(_manager, MultiProviderManager):
            # In multi-provider mode, use the 'chat' role's provider
            chat_mgr = _manager.get_for_role("chat")
            chat_mgr.set_model(
                chat_cfg.provider, chat_cfg.model,
                keep_alive=chat_cfg.keep_alive, max_tokens=chat_cfg.max_tokens,
                temperature=chat_cfg.temperature, timeout=chat_cfg.timeout,
                num_ctx=getattr(chat_cfg, "num_ctx", None),
            )
            _active_mgr = chat_mgr
        else:
            _manager.set_model(
                chat_cfg.provider, chat_cfg.model,
                keep_alive=chat_cfg.keep_alive, max_tokens=chat_cfg.max_tokens,
                temperature=chat_cfg.temperature, timeout=chat_cfg.timeout,
                num_ctx=getattr(chat_cfg, "num_ctx", None),
            )
            _active_mgr = _manager
    except Exception:
        _active_mgr = _manager if isinstance(_manager, ModelManager) else _manager.active

    # Optional one-shot override for this turn only.
    if req.model and ":" in req.model:
        prov, mod = req.model.split(":", 1)
        try:
            if isinstance(_manager, MultiProviderManager):
                _active_mgr.set_model(prov, mod)
            else:
                _manager.set_model(prov, mod)
        except Exception:
            pass

    role = req.role if req.role in PERSONAS else "chat"
    system = PERSONAS[role]
    if req.context:
        system += f"\n\nRecent conversation:\n{req.context}"
    messages = [Message(role="system", content=system),
                Message(role="user", content=req.message)]

    async def gen():
        # Tokens are PUSHED through a queue as the worker produces them. The
        # previous version appended to a shared string and woke every 50ms to
        # diff it, so each token waited up to 50ms for a poll it could have
        # skipped — visible as a reply that trickles out slower than the model
        # is actually generating.
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        parts: list = []
        DONE = object()

        def worker():
            try:
                for chunk in _active_mgr.stream(messages, tools=None):
                    if chunk.delta:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk.delta)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, f"[error: {e}]")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, DONE)

        loop.run_in_executor(None, worker)
        while True:
            item = await queue.get()
            if item is DONE:
                break
            parts.append(item)
            yield {"event": "token", "data": item}

        text = "".join(parts)
        # Leave the fast chat model configured. Restoring the heavy coder model
        # here forced Ollama to swap models in VRAM between every chat turn; the
        # task/pipeline paths set the model they need when they run.
        yield {"event": "done", "data": json.dumps({"response": text, "tool_calls": 0})}
    return EventSourceResponse(gen())





def _orchestrator_models() -> list:
    try:
        from ..orchestrator.multi_agent import MODEL_PLAN
        return [MODEL_PLAN[k] for k in ("architect", "orchestrator", "executioner", "frontend")]
    except Exception:
        return []


@app.get("/context/summary")
def context_summary(project_path: Optional[str] = None):
    from ..core.context import ContextEngine
    root = Path(project_path) if project_path else _settings.workspace_dir
    ce = ContextEngine(root, logger=_logger)
    ce.index()
    return {"summary": ce.summary(), "languages": ce.languages()}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    _check_auth(request)
    from ..agents.personas import PERSONAS
    from ..models.types import Message
    # Optional one-shot model switch before replying.
    if req.model and ":" in req.model:
        prov, mod = req.model.split(":", 1)
        try:
            _manager.set_model(prov, mod)
        except Exception:
            pass
    role = req.role if req.role in PERSONAS else "chat"
    system = PERSONAS[role]
    if req.context:
        system += f"\n\nRecent conversation:\n{req.context}"
    # Conversational turn: NO tools passed, so the model replies in natural
    # language instead of emitting tool calls. This is what makes chat work
    # (local code models like Qwen-Coder force tool calls whenever tools are
    # present).
    messages = [Message(role="system", content=system),
                Message(role="user", content=req.message)]
    # Use the active chat provider
    if isinstance(_manager, MultiProviderManager):
        active_mgr = _manager.get_for_role("chat")
    else:
        active_mgr = _manager
    resp = active_mgr.complete(messages, tools=None)
    text = (resp.text or "").strip()
    # If the model still emitted a lone JSON tool call, return a friendly note
    # instead of raw JSON.
    if text.startswith("{") and '"name"' in text:
        text = "Sure — tell me what you'd like me to build or fix, and tick " \
               "\"run as autonomous task\" (or press Ctrl+Enter) to execute it."
    return {"response": text, "tool_calls": 0,
            "active": {"provider": active_mgr.cfg.provider, "model": active_mgr.model_name}}


@app.post("/run")
async def run(req: RunRequest, request: Request):
    _check_auth(request)
    engine = AutonomousEngine(model_manager=_manager, memory=_memory,
                              logger=_logger)
    result = engine.run(
        request=req.request,
        project_path=req.project_path,
        verify_command=req.verify_command,
        require_approval=req.require_approval,
        auto_commit=req.auto_commit,
    )
    return _result_to_dict(result)


@app.post("/run/stream")
async def run_stream(req: RunRequest, request: Request):
    """Server-Sent Events stream of engine events, ending with the result."""
    _check_auth(request)
    from sse_starlette.sse import EventSourceResponse

    async def event_gen():
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(e: dict):
            queue.put_nowait(e)

        engine = AutonomousEngine(model_manager=_manager, memory=_memory,
                                  logger=_logger, on_event=on_event)
        # Run the (blocking) engine in a thread so we can stream events.
        future = loop.run_in_executor(
            None,
            lambda: engine.run(
                request=req.request,
                project_path=req.project_path,
                verify_command=req.verify_command,
                require_approval=req.require_approval,
                auto_commit=req.auto_commit,
            ),
        )
        # Stream events as they arrive.
        while True:
            try:
                e = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield {"event": "event", "data": json.dumps(e)}
            except asyncio.TimeoutError:
                if future.done():
                    break
        result = future.result()
        yield {"event": "result", "data": json.dumps(_result_to_dict(result))}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_gen())


# --------------------------------------------------------------------------
# 4-AGENT SEQUENTIAL PIPELINE (Architect -> Orchestrator -> Executioner -> Frontend)
# Each model owns one task; models hot-swap on the 8GB GPU (no concurrent loads).
# --------------------------------------------------------------------------
class OrchestrateRequest(BaseModel):
    request: str
    project_path: Optional[str] = None
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"


@app.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest, request: Request):
    """Server-Sent Events stream of the multi-agent run, ending with the result."""
    _check_auth(request)
    from sse_starlette.sse import EventSourceResponse

    async def event_gen():
        # EVERYTHING here runs after the SSE response has already begun, so an
        # uncaught exception cannot become an HTTP error — the connection just
        # drops and the browser reports a bare "network error" with no cause.
        # That is why a missing model, a bad project path or any crash inside
        # the pipeline all looked identical and unactionable in the UI.
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(e: dict):
            queue.put_nowait(e)

        def _fail(stage: str, exc: BaseException):
            _logger.error("orchestrate", f"{stage}: {type(exc).__name__}: {exc}")
            return {"event": "error", "data": json.dumps({
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": _orchestrate_hint(exc, req),
            })}

        try:
            orch = MultiAgentOrchestrator(
                base_url=req.base_url,
                provider=req.provider,
                workspace_root=Path(req.project_path) if req.project_path else None,
                on_event=on_event,
            )
        except Exception as e:  # noqa: BLE001
            yield _fail("starting the pipeline", e)
            yield {"event": "done", "data": "{}"}
            return

        _ACTIVE_RUNS[id(orch)] = orch
        future = loop.run_in_executor(
            None, lambda: orch.run(req.request),
        )
        # Watchdog: if the run exceeds the cap, stop it so the client never hangs.
        RUN_CAP_S = 600  # 10 min hard ceiling for the whole pipeline
        started = time.time()
        while True:
            try:
                e = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield {"event": "event", "data": json.dumps(e)}
            except asyncio.TimeoutError:
                if future.done():
                    break
                if time.time() - started > RUN_CAP_S:
                    orch.request_cancel()
                    yield {"event": "event", "data": json.dumps(
                        {"kind": "progress", "source": "orchestrator",
                         "message": "⏱ Run exceeded the time limit and was stopped.",
                         "data": {"pct": 100, "stage": "orchestrator"}})}
                    break
        _ACTIVE_RUNS.pop(id(orch), None)
        try:
            result = future.result()
        except Exception as e:  # noqa: BLE001
            yield _fail("running the pipeline", e)
            yield {"event": "done", "data": "{}"}
            return
        yield {"event": "result",
               "data": json.dumps(result.to_dict())}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_gen())


def _orchestrate_hint(exc: BaseException, req) -> str:
    """Turn a raw exception into something the user can act on."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "connect" in text or "refused" in text or "connecterror" in text:
        return (f"Ollama does not appear to be running at {req.base_url or 'http://localhost:11434'}. "
                f"Start it with `ollama serve`.")
    if "404" in text or "not found" in text:
        return ("A model the pipeline asked for is not installed. Run "
                "`ollama list` to see what you have, then `ollama pull <model>`.")
    if "timeout" in text or "timed out" in text:
        return ("The model took longer than the configured timeout. Raise "
                "model.timeout in config/settings.yaml, or use a smaller model.")
    if "memory" in text or "vram" in text or "oom" in text:
        return ("The GPU ran out of memory. Lower model.num_ctx in "
                "config/settings.yaml, or use a smaller model.")
    if "permission" in text or "refused:" in text:
        return "A path or command was blocked by the security layer. See the Log tab."
    return "See the Log tab in the UI, or the server console, for the full traceback."


# Registry of in-flight orchestrator runs so the client can Stop them.
_ACTIVE_RUNS: dict = {}


@app.post("/orchestrate/cancel")
async def orchestrate_cancel(request: Request):
    """Cancel any currently running pipeline (Stop button)."""
    _check_auth(request)
    n = 0
    for orch in list(_ACTIVE_RUNS.values()):
        try:
            orch.request_cancel()
            n += 1
        except Exception:
            pass
    return {"ok": True, "cancelled": n}


# --------------------------------------------------------------------------
# Telegram channel bridge status (the bot itself runs via `yacine telegram`).
# --------------------------------------------------------------------------
class TelegramTestRequest(BaseModel):
    token: Optional[str] = None
    channel: Optional[str] = None


# -------------------------------------------------------------------------
# Local filesystem browser — lets the UI offer a "select folder" picker
# for per-chat / per-project working directories.
# -------------------------------------------------------------------------
class BrowseRequest(BaseModel):
    path: Optional[str] = None  # directory to list; defaults to user home


class MkdirRequest(BaseModel):
    parent: str            # directory in which to create the new folder
    name: str              # new folder name


@app.post("/api/mkdir")
async def mkdir(req: MkdirRequest, request: Request):
    """Create a new folder inside `parent`. Returns the created path."""
    _check_auth(request)
    import os
    name = (req.name or "").strip().replace("\\", "/").replace("/", "").replace("..", "")
    if not name:
        return {"ok": False, "error": "invalid name"}
    dest = os.path.join(req.parent or os.path.expanduser("~"), name)
    try:
        os.makedirs(dest, exist_ok=True)
        return {"ok": True, "path": dest}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/browse")
async def browse(req: BrowseRequest, request: Request):
    _check_auth(request)
    import os
    root = req.path or os.path.expanduser("~")
    try:
        entries = []
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                is_dir = False
            if name.startswith("."):
                continue
            entries.append({"name": name, "path": full, "is_dir": is_dir})
        # Directories first.
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = os.path.dirname(root) if os.path.abspath(root) != os.path.abspath(os.path.dirname(root)) else None
        return {"path": root, "parent": parent, "entries": entries[:400]}
    except Exception as e:
        return {"path": root, "parent": None, "entries": [], "error": str(e)}
@app.post("/api/telegram/test")
async def telegram_test(req: TelegramTestRequest, request: Request):
    """Send a test message to the configured Telegram channel."""
    _check_auth(request)
    try:
        from ..channels.telegram import TelegramChannel
        from ..core.config import get_settings
        cfg = get_settings().telegram
        cfg.bot_token = req.token or cfg.bot_token or ""
        cfg.channel = req.channel or cfg.channel or ""
        chan = TelegramChannel(cfg, logger=_logger)
        me = chan._api("getMe")["result"]
        chan.send("✅ Y.A.C.I.N.E Telegram bridge test message.")
        return {"ok": True, "bot": me.get("username"), "channel": cfg.channel}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/memory")
def list_memory(limit: int = 50):
    return {"memories": [m.to_dict() for m in _memory.all()[:limit]]}


@app.post("/memory")
def add_memory(req: dict, request: Request):
    _check_auth(request)
    content = req.get("content", "")
    category = req.get("category", "fact")
    if not content:
        raise HTTPException(400, "content required")
    m = _memory.add(content, category=category, source="api")
    return {"memory": m.to_dict()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _check_ws_auth(ws):
        await ws.close(code=1008, reason="Invalid or missing API key")
        return
    await ws.accept()
    # Subscribe a callback that forwards engine events to this socket.
    async def forward(e):
        try:
            await ws.send_text(json.dumps(e.to_dict()))
        except Exception:
            pass
    unsub = _bus.subscribe(forward)
    try:
        while True:
            # Keep the socket alive; client can send pings.
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"kind":"pong"}')
    except WebSocketDisconnect:
        unsub()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _result_to_dict(r: TaskResult) -> dict:
    return {
        "task_id": r.task_id,
        "request": r.request,
        "success": r.success,
        "verdict": r.verdict,
        "plan": r.plan,
        "steps_run": r.steps_run,
        "self_heals": r.self_heals,
        "tool_calls": r.tool_calls,
        "artifacts": r.artifacts,
        "workspace": r.workspace,
        "duration": round(r.duration, 2),
        "error": r.error,
        "quality": r.quality,
        "review": r.review,
        "security": r.security,
        "docs": r.docs,
        "research": getattr(r, "research", ""),
        "logs": r.logs,
    }


# --------------------------------------------------------------------------
# File / folder upload (Phase F1) — for the Desktop GUI "Upload" button.
# --------------------------------------------------------------------------
@app.post("/api/upload")
async def upload_files(request: Request):
    """Accept multipart uploads (files or a folder) and save them into the
    configured workspace/uploads dir. Returns the list of saved paths."""
    from pathlib import PurePosixPath

    upload_dir = _settings.workspace_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        form = await request.form()
        for key in form:
            items = form.getlist(key)
            for item in items:
                if hasattr(item, "filename"):
                    rel = PurePosixPath(getattr(item, "filename", "upload.bin"))
                    dest = upload_dir / rel.name
                    data = await item.read()
                    dest.write_bytes(data)
                    saved.append(str(dest))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "saved": saved}


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    # flush=True matters in the frozen .exe: stdout is block-buffered when it
    # is not a terminal, so without it this banner can sit unseen in the buffer
    # while the user stares at a console that never shows them the API key.
    banner = [
        f"\n  UI:      http://{host}:{port}/ui   (opens without a key)",
        f"  API key: {API_KEY}",
        f"  source:  {key_source()}",
        "  External API clients send it as the X-API-Key header.\n",
    ]
    if host not in ("127.0.0.1", "localhost", "::1"):
        banner.append(f"  WARNING: binding to {host} exposes an agent that runs "
                      f"shell commands to your network.\n")
    for line in banner:
        print(line, flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
