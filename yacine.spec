# PyInstaller spec for Y.A.C.I.N.E — produces a standalone yacine.exe
# Usage: pyinstaller yacine.spec
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()
added_files = [
    (str(ROOT / "config"), "config"),
    (str(ROOT / "tests" / "fixtures" / "scripted_task.yaml"), "."),
    (str(ROOT / "yacine" / "ui" / "static"), "yacine/ui/static/static"),
    (str(ROOT / "plugins"), "plugins"),
    (str(ROOT / "README.md"), "."),
]

block_cipher = None

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        "yacine.core", "yacine.core.config", "yacine.core.logger",
        "yacine.core.security", "yacine.core.context", "yacine.core.engine",
        "yacine.core.quality", "yacine.core.coverage",
        "yacine.models", "yacine.models.types", "yacine.models.base",
        "yacine.models.ollama", "yacine.models.openai_compat",
        "yacine.models.scripted", "yacine.models.manager",
        "yacine.memory", "yacine.memory.store", "yacine.memory.longterm",
        "yacine.tools", "yacine.tools.base", "yacine.tools.sandbox",
        "yacine.tools.filesystem", "yacine.tools.code_edit", "yacine.tools.terminal",
        "yacine.tools.internet", "yacine.tools.browser", "yacine.tools.git",
        "yacine.tools.db", "yacine.tools.vision", "yacine.tools.voice",
        "yacine.tools.mcp", "yacine.tools.registry",
        "yacine.agents", "yacine.agents.personas", "yacine.agents.planner",
        "yacine.agents.agent",
        "yacine.orchestrator", "yacine.orchestrator.multi_agent",
        "yacine.orchestrator.prompts",
        "yacine.api", "yacine.api.server", "yacine.ui", "yacine.ui.backend",
        "yacine.cli",
        "uvicorn", "uvicorn.logging", "uvicorn.protocols", "uvicorn.protocols.http",
        "uvicorn.protocols.websockets", "uvicorn.lifespan", "uvicorn.server",
        "starlette", "fastapi", "sse_starlette",
        "yaml", "rich",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="yacine_server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # keep console so CLI output is visible
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
