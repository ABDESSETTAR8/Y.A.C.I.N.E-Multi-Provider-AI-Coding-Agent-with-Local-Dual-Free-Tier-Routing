"""Configuration management for Y.A.C.I.N.E.

Loads settings from a YAML file + environment variables, with sensible
defaults so the system runs out-of-the-box. All paths are resolved to
absolute paths relative to the project root.
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


def _app_base_dir() -> Path:
    """Directory the app really runs from.

    When frozen by PyInstaller (sys.frozen), sys.executable is the .exe and its
    folder is the app root (config/, data/, etc. are bundled alongside it).
    Otherwise it's the project root (repo dir)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


DEFAULT_PROJECT_ROOT = _app_base_dir()


# --------------------------------------------------------------------------
# Permission model (security: every sensitive action is gated)
# --------------------------------------------------------------------------
class Permissions(BaseModel):
    read_file: bool = True
    write_file: bool = True
    execute_command: bool = True
    delete_file: bool = True
    internet_access: bool = True
    git_push: bool = False  # off by default: never push without explicit opt-in

    def to_dict(self) -> Dict[str, bool]:
        return self.model_dump()

    def check(self, name: str) -> bool:
        return bool(getattr(self, name, False))


# --------------------------------------------------------------------------
# Model provider config
# --------------------------------------------------------------------------
class ModelConfig(BaseModel):
    provider: str = Field(default="ollama", description="ollama | openai | scripted")
    model: str = Field(default="qwen2.5-coder:7b", description="Model tag/name")
    base_url: str = Field(default="http://localhost:11434", description="Provider base URL")
    api_key: str = Field(default="", description="API key (for OpenAI-compatible providers)")
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 300
    # Context window handed to the provider. Ollama otherwise applies its own
    # default (4096, or 2048 on older builds) and silently truncates anything
    # longer, which is the usual cause of a model "ignoring" its instructions.
    # Raise it to the model's real window; lower it if VRAM is tight.
    num_ctx: int = 8192
    # How long Ollama keeps the model resident in VRAM after a response. On an
    # 8 GB GPU use a short window (e.g. "1m") or force immediate eviction by
    # passing keep_alive=0 to manager.complete(). The Orchestrator drives this
    # directly to enforce sequential hot-swapping and avoid OOM.
    keep_alive: Optional[str] = None
    # For the 'scripted' provider used in tests / offline dev
    script_path: Optional[str] = None


class ChatModelConfig(ModelConfig):
    """Lightweight, fast model used for the conversational chat tab.

    Kept SEPARATE from the heavy coder model: chat should be snappy, so it
    defaults to a small model (qwen2.5:3b) that loads in ~2s and stays warm
    between turns (keep_alive). Heavy coding work still uses the main model
    (qwen2.5-coder:7b) via the autonomous task / pipeline paths.
    """
    model: str = Field(default="qwen2.5:3b", description="Fast chat model")
    keep_alive: Optional[str] = "15m"   # stay resident for quick follow-ups
    max_tokens: int = 1024              # short, focused chat replies
    temperature: float = 0.3
    num_ctx: int = 4096                 # chat turns are short; save VRAM


# --------------------------------------------------------------------------
# Top-level settings
# --------------------------------------------------------------------------
class TelegramConfig(BaseModel):
    """Telegram Bot API bridge for Y.A.C.I.N.E channels.

    The bot posts pipeline results (plan, micro-tasks, execution summary) to a
    Telegram channel/group, and can accept `/orchestrate <request>` commands.
    Token stays in config / env (YACINE_TELEGRAM_TOKEN), never logged.
    """
    enabled: bool = False
    bot_token: str = ""                 # @BotFather token (or set YACINE_TELEGRAM_TOKEN)
    channel: str = ""                  # channel username (@channel) or numeric chat_id
    poll_timeout: int = 30             # getUpdates long-poll seconds
    allowed_users: List[str] = Field(default_factory=list)  # Telegram user ids allowed to command
    stream_to_channel: bool = True     # post live pipeline updates to the channel


class Settings(BaseModel):
    project_root: Path = DEFAULT_PROJECT_ROOT
    workspace_dir: Path = Field(default=Path("workspace"))
    data_dir: Path = Field(default=Path("data"))
    logs_dir: Path = Field(default=Path("logs"))
    model: ModelConfig = Field(default_factory=ModelConfig)
    permissions: Permissions = Field(default_factory=Permissions)
    max_plan_iterations: int = 20
    max_self_heal_iterations: int = 10
    stream: bool = True
    telemetry: bool = True
    # Plugin / extension directories
    plugins_dir: Path = Field(default=Path("plugins"))
    # MCP servers: name -> {command: [...], env: {...}}
    mcp_servers: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Phase A engine behaviour toggles
    enable_review: bool = True          # run Reviewer + Security agents post-implement
    enable_docs: bool = True            # run Docs agent post-implement
    enable_code_quality: bool = True    # run black/ruff/eslint/prettier enforcement
    enable_research: bool = True        # run Research agent when external knowledge may help
    coverage_min_pct: int = 0           # 0 = disabled; else fail verify below threshold
    require_approval: bool = False       # pause loop for user sign-off between plan & implement
    # Telegram channel integration (Bot API bridge for Y.A.C.I.N.E).
    telegram: "TelegramConfig" = Field(default_factory=lambda: TelegramConfig())
    # Fast model for the conversational chat tab (snappy, stays warm).
    chat_model: "ChatModelConfig" = Field(default_factory=lambda: ChatModelConfig())

    def model_post_init(self, __context: Any) -> None:
        # Resolve relative dirs against project root.
        for fld in ("workspace_dir", "data_dir", "logs_dir", "plugins_dir"):
            p: Path = getattr(self, fld)
            if not p.is_absolute():
                setattr(self, fld, self.project_root / p)
        # Create dirs lazily.
        for fld in ("workspace_dir", "data_dir", "logs_dir"):
            d: Path = getattr(self, fld)
            d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: Optional[str] = None) -> Settings:
    """Load configuration from a YAML file, then apply environment overrides.

    Environment variables (YACINE_* ) override file values:
      YACINE_MODEL_PROVIDER, YACINE_MODEL_NAME, YACINE_MODEL_BASE_URL,
      YACINE_PERM_GIT_PUSH, YACINE_PROJECT_ROOT, ...
    """
    path = Path(config_path or os.environ.get("YACINE_CONFIG",
                                              DEFAULT_PROJECT_ROOT / "config" / "settings.yaml"))
    data: Dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    # Environment overrides ------------------------------------------------
    env = os.environ
    if env.get("YACINE_MODEL_PROVIDER"):
        data.setdefault("model", {})["provider"] = env["YACINE_MODEL_PROVIDER"]
    if env.get("YACINE_MODEL_NAME"):
        data.setdefault("model", {})["model"] = env["YACINE_MODEL_NAME"]
    if env.get("YACINE_MODEL_BASE_URL"):
        data.setdefault("model", {})["base_url"] = env["YACINE_MODEL_BASE_URL"]
    if env.get("YACINE_MODEL_API_KEY"):
        data.setdefault("model", {})["api_key"] = env["YACINE_MODEL_API_KEY"]
    if env.get("YACINE_PROJECT_ROOT"):
        data["project_root"] = Path(env["YACINE_PROJECT_ROOT"])
    if env.get("YACINE_PERM_GIT_PUSH"):
        data.setdefault("permissions", {})["git_push"] = env["YACINE_PERM_GIT_PUSH"].lower() in ("1", "true", "yes")
    if env.get("YACINE_PERM_INTERNET"):
        data.setdefault("permissions", {})["internet_access"] = env["YACINE_PERM_INTERNET"].lower() in ("1", "true", "yes")

    settings = Settings(**data)
    # Stash the resolved config path.
    settings.__config_path__ = path  # type: ignore[attr-defined]
    return settings


def save_config(settings: Settings, config_path: Optional[str] = None) -> Path:
    path = Path(config_path or getattr(settings, "__config_path__",
                                       DEFAULT_PROJECT_ROOT / "config" / "settings.yaml"))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Convert Path objects to strings for YAML serialization.
    payload = json.loads(settings.model_dump_json())
    payload["project_root"] = str(payload["project_root"])
    for k in ("workspace_dir", "data_dir", "logs_dir", "plugins_dir"):
        payload[k] = str(payload[k])
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    return path


# A module-level singleton accessor (lazily initialised).
_CONFIG: Optional[Settings] = None


def get_settings(config_path: Optional[str] = None) -> Settings:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config(config_path)
    return _CONFIG


def reset_settings() -> None:
    global _CONFIG
    _CONFIG = None
