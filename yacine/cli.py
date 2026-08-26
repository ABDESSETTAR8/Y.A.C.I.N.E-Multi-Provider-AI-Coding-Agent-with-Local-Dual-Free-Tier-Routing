"""Y.A.C.I.N.E command-line interface.

A modern, dark-friendly terminal UI built with Rich. Commands:
  yacine chat        -> interactive single-agent chat (tool-enabled)
  yacine run         -> run a full autonomous task (self-healing loop)
  yacine serve       -> start the REST + WebSocket API
  yacine memory      -> list / add / search / forget memories
  yacine context     -> index & summarise a project
  yacine config      -> print / set config (model, permissions)
  yacine models      -> check provider availability
  yacine doctor      -> environment sanity check

The CLI is the primary local interface; the desktop UI (Phase 5) will wrap
the same engine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.prompt import Prompt

from .core.config import get_settings, load_config, save_config, ModelConfig, Permissions
from .core.logger import get_logger, get_bus
from .core.engine import AutonomousEngine
from .models.manager import ModelManager
from .memory.store import MemoryStore
from .agents.personas import PERSONAS

console = Console()
logger = get_logger("cli")


def _print_banner() -> None:
    console.print(Panel(
        "[bold cyan]Y.A.C.I.N.E[/bold cyan]  —  Yet Another Cognitive Intelligent "
        "Neural Engineer\n[dim]Local, privacy-first autonomous software engineer[/dim]",
        border_style="cyan", expand=False))


def cmd_chat(args) -> int:
    _print_banner()
    settings = get_settings()
    manager = ModelManager(logger=logger)
    from ..tools.registry import build_tool_registry, make_sandbox
    from ..agents.agent import run_agent
    sandbox = make_sandbox("cli_chat", logger=logger)
    tools = build_tool_registry(sandbox)
    memory = MemoryStore(logger=logger)
    role = args.role if args.role in PERSONAS else "coder"
    console.print(f"[green]Chat mode[/green] · role=[cyan]{role}[/cyan] · "
                  f"model=[cyan]{manager.model_name}[/cyan] · provider=[cyan]{manager.cfg.provider}[/cyan]")
    console.print("[dim]Type 'exit' to quit. Tools are available to the agent.[/dim]\n")
    while True:
        try:
            user = Prompt.ask("[bold magenta]you[/bold magenta]")
        except (EOFError, KeyboardInterrupt):
            break
        if user.strip().lower() in ("exit", "quit"):
            break
        out = run_agent(manager, tools, role=role, task=user,
                        logger=logger, memory=memory, max_tool_iters=14)
        console.print(Panel(Markdown(out["text"] or "(no response)"),
                            title=f"{role} · {out['tool_calls'] if 'tool_calls' in out else len(out['tool_log'])} tool calls",
                            border_style="green"))
    return 0


def cmd_run(args) -> int:
    _print_banner()
    settings = get_settings()
    engine = AutonomousEngine(logger=logger)
    console.print(f"[bold]Autonomous task:[/bold] {args.request}\n")
    with console.status("[cyan]Working autonomously (think → plan → implement → test → heal → deliver)...[/cyan]"):
        result = engine.run(
            request=args.request,
            project_path=args.project,
            verify_command=args.verify,
            auto_commit=not args.no_commit,
        )
    _render_result(result)
    return 0 if result.success else 1


def _render_result(result) -> None:
    status = "[green]SUCCESS ✅[/green]" if result.success else "[red]INCOMPLETE ❌[/red]"
    console.print(Panel(
        f"{status}\n[dim]{result.verdict}[/dim]\n\n"
        f"Steps run: {result.steps_run} · Self-heals: {result.self_heals} · "
        f"Tool calls: {result.tool_calls} · Duration: {result.duration:.1f}s\n"
        f"Workspace: {result.workspace}",
        title=f"Task {result.task_id}", border_style="green" if result.success else "red"))
    if result.plan:
        console.print(Markdown("## Plan\n" + result.plan[:2000]))
    if result.artifacts:
        console.print("[bold]Artifacts:[/bold]")
        for a in result.artifacts[:25]:
            console.print(f"  • {a}")
    if result.logs:
        console.print("[bold]Execution log (tail):[/bold]")
        for line in result.logs[-15:]:
            console.print(f"  [dim]{line[:200]}[/dim]")


def cmd_serve(args) -> int:
    try:
        from ..api.server import run_server
    except ImportError:
        # Frozen / one-file build: module path may differ.
        import importlib
        run_server = importlib.import_module("yacine.api.server").run_server
    console.print(f"[green]Starting Y.A.C.I.N.E API on http://{args.host}:{args.port}[/green]")
    run_server(host=args.host, port=args.port)
    return 0


def cmd_telegram(args) -> int:
    """Run the Telegram channel bridge."""
    from .channels.telegram import build_telegram_channel
    from .orchestrator import MultiAgentOrchestrator
    console.print("[bold]Y.A.C.I.N.E Telegram channel[/bold]")
    chan = build_telegram_channel(logger=logger, token=args.token or "", channel=args.channel or "")
    if not chan:
        console.print("[red]Telegram not enabled.[/red] Set telegram.enabled=true in "
                      "config/settings.yaml (and bot_token/channel), or pass --token/--channel.")
        return 1
    if args.token:
        chan.token = args.token
        chan.cfg.enabled = True
    if args.channel:
        chan.chat = args.channel
        chan.cfg.enabled = True

    if args.once:
        chan.send("✅ Y.A.C.I.N.E Telegram bridge test message.")
        console.print("[green]Test message sent. Bridge reachable.[/green]")
        return 0

    def run_pipeline(request: str) -> None:
        orch = MultiAgentOrchestrator(on_event=chan.on_event)
        res = orch.run(request)
        chan.send(
            f"✅ *Done* · {len(res.micro_tasks)} micro-tasks · verified={res.verification_passed}\n\n"
            f"{res.summary or res.error}"
        )

    try:
        chan.run_polling(run_pipeline)
    except KeyboardInterrupt:
        console.print("[dim]stopped[/dim]")
    return 0

def cmd_orchestrate(args) -> int:
    """Run the 4-stage local multi-agent pipeline (Architect -> Orchestrator
    -> Executioner -> Frontend) sequentially with VRAM hot-swapping."""
    from .orchestrator import MultiAgentOrchestrator
    _print_banner()
    console.print(f"[bold]Orchestrate:[/bold] {args.request}\n")
    orch = MultiAgentOrchestrator(
        base_url=args.base_url,
        provider=args.provider,
        script_path=args.script if args.provider == "scripted" else None,
    )
    with console.status("[cyan]Sequential pipeline (hot-swapping models on 8GB VRAM)...[/cyan]"):
        result = orch.run(args.request)
    console.print(Panel(
        f"{'SUCCESS ✅' if result.success else 'INCOMPLETE ❌'}\n"
        f"[dim]{result.summary or result.error}[/dim]\n\n"
        f"Micro-tasks: {len(result.micro_tasks)} · Verified: {result.verification_passed} · "
        f"Duration: {result.duration:.1f}s",
        title="Orchestrator", border_style="green" if result.success else "red"))
    if result.plan:
        console.print(Markdown("## Architect plan\n" + result.plan[:1500]))
    return 0 if result.success else 1


def cmd_memory(args) -> int:
    mem = MemoryStore(logger=logger)
    if args.action == "list":
        t = Table(title="Memory")
        t.add_column("id"); t.add_column("category"); t.add_column("content")
        for m in mem.all():
            t.add_row(m.id, m.category, m.content[:80])
        console.print(t)
    elif args.action == "add":
        m = mem.add(args.content, category=args.category)
        console.print(f"[green]Added[/green] {m.id}")
    elif args.action == "search":
        for m in mem.search(args.content, limit=5):
            console.print(f"• [{m.category}] {m.content}")
    elif args.action == "forget":
        ok = mem.forget(args.id)
        console.print("Removed" if ok else "Not found")
    return 0


def cmd_context(args) -> int:
    from .core.context import ContextEngine
    root = Path(args.path)
    ce = ContextEngine(root, logger=logger)
    ce.index()
    console.print(Markdown("```\n" + ce.summary() + "\n```"))
    return 0


def cmd_config(args) -> int:
    settings = load_config()
    if args.show:
        console.print_json(json.dumps(settings.model_dump(mode="json"), indent=2))
    elif args.set_model:
        settings.model.model = args.set_model
        save_config(settings)
        console.print(f"[green]Model set to[/green] {settings.model.model}")
    elif args.set_provider:
        settings.model.provider = args.set_provider
        save_config(settings)
        console.print(f"[green]Provider set to[/green] {settings.model.provider}")
    elif args.set_perm:
        # expects key=value
        k, _, v = args.set_perm.partition("=")
        if hasattr(settings.permissions, k):
            setattr(settings.permissions, k, v.lower() in ("1", "true", "yes"))
            save_config(settings)
            console.print(f"[green]{k} = {v}[/green]")
    else:
        console.print(settings.model_dump_json(indent=2))
    return 0


def cmd_models(args) -> int:
    settings = get_settings()
    mgr = ModelManager(logger=logger)
    t = Table(title="Model providers")
    t.add_column("provider"); t.add_column("model"); t.add_column("available")
    for prov in ("ollama", "openai", "scripted"):
        try:
            from .models.manager import _PROVIDERS
            cls = _PROVIDERS[prov]
            inst = cls(settings.model, logger=logger)
            avail = "yes" if inst.is_available() else "no"
        except Exception as e:
            avail = f"error: {e}"
        t.add_row(prov, settings.model.model if prov == settings.model.provider else "-", avail)
    console.print(t)
    console.print(f"\nActive provider: [cyan]{mgr.cfg.provider}[/cyan] · model: [cyan]{mgr.model_name}[/cyan]")
    return 0


def cmd_doctor(args) -> int:
    t = Table(title="Environment check")
    t.add_column("check"); t.add_column("status"); t.add_column("detail")
    import shutil, subprocess
    py = shutil.which("python") or shutil.which("python3")
    t.add_row("python", "ok" if py else "missing", str(py))
    ollama = shutil.which("ollama")
    t.add_row("ollama", "found" if ollama else "not found",
              "install from https://ollama.com" if not ollama else ollama)
    node = shutil.which("node")
    t.add_row("node", "found" if node else "not found", str(node))
    git = shutil.which("git")
    t.add_row("git", "found" if git else "not found", str(git))
    mgr = ModelManager(logger=logger)
    t.add_row("model backend", "reachable" if mgr.available() else "not reachable",
              f"{mgr.cfg.provider} @ {mgr.cfg.base_url}")
    console.print(t)
    return 0


def cmd_upload(args) -> int:
    """Copy uploaded files/folders into the workspace/uploads dir (Phase F1)."""
    from .core.config import get_settings
    settings = get_settings()
    dest = settings.workspace_dir / "uploads"
    dest.mkdir(parents=True, exist_ok=True)
    import shutil
    count = 0
    for src in args.paths:
        s = Path(src)
        if not s.exists():
            console.print(f"[red]not found:[/red] {src}")
            continue
        if s.is_dir():
            shutil.copytree(s, dest / s.name, dirs_exist_ok=True)
        else:
            shutil.copy2(s, dest / s.name)
        count += 1
        console.print(f"[green]uploaded[/green] {src}")
    console.print(f"\n[cyan]{count}[/cyan] item(s) -> {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yacine", description="Y.A.C.I.N.E autonomous engineer")
    sub = p.add_subparsers(dest="command")

    pc = sub.add_parser("chat", help="Interactive agent chat")
    pc.add_argument("--role", default="coder", choices=list(PERSONAS.keys()))

    pr = sub.add_parser("run", help="Run a full autonomous task")
    pr.add_argument("request", help="What to build/fix")
    pr.add_argument("--project", default=None, help="Project directory to operate in")
    pr.add_argument("--verify", default=None, help="Verification command (e.g. 'pytest -q')")
    pr.add_argument("--no-commit", action="store_true", help="Do not git-commit")

    ps = sub.add_parser("serve", help="Start REST + WebSocket API (and Desktop UI at /ui)")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8765)

    po = sub.add_parser("orchestrate", help="Run the 4-agent sequential pipeline (Architect->Orchestrator->Executioner->Frontend)")
    po.add_argument("request", help="What to build/fix")
    po.add_argument("--provider", default="ollama", choices=["ollama", "scripted"],
                    help="Model backend (scripted=offline fixture)")
    po.add_argument("--base-url", default="http://localhost:11434", help="Ollama base URL")
    po.add_argument("--script", default="tests/fixtures/orchestrator_pipeline.yaml",
                    help="Scripted fixture path (provider=scripted)")

    pu = sub.add_parser("upload", help="Upload a file or folder into the workspace (for Vision/PDF tasks)")
    pu.add_argument("paths", nargs="+", help="Files or folders to upload")

    pt = sub.add_parser("telegram", help="Run the Telegram channel bridge (bot posts pipeline results + accepts /orchestrate)")
    pt.add_argument("--token", default=None, help="BotFather token (or set YACINE_TELEGRAM_TOKEN)")
    pt.add_argument("--channel", default=None, help="Channel @username or numeric chat_id")
    pt.add_argument("--once", action="store_true", help="Post a test message and exit (no polling)")

    pm = sub.add_parser("memory", help="Memory management")
    pm.add_argument("action", choices=["list", "add", "search", "forget"])
    pm.add_argument("--content", default="")
    pm.add_argument("--category", default="fact")
    pm.add_argument("--id", default="")

    pctx = sub.add_parser("context", help="Summarise a project")
    pctx.add_argument("path", help="Project directory")

    pcf = sub.add_parser("config", help="Show/set configuration")
    pcf.add_argument("--show", action="store_true")
    pcf.add_argument("--set-model", default=None)
    pcf.add_argument("--set-provider", default=None)
    pcf.add_argument("--set-perm", default=None, help="key=value (e.g. git_push=true)")

    pmod = sub.add_parser("models", help="Check model providers")
    pdoc = sub.add_parser("doctor", help="Environment sanity check")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        _print_banner()
        parser.print_help()
        return 0
    handlers = {
        "chat": cmd_chat, "run": cmd_run, "serve": cmd_serve,
        "memory": cmd_memory, "context": cmd_context, "config": cmd_config,
        "models": cmd_models, "doctor": cmd_doctor, "upload": cmd_upload,
        "orchestrate": cmd_orchestrate,
        "telegram": cmd_telegram,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
