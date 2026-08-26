"""Desktop GUI backend (Phase E).

Serves a self-contained local web UI (no build step) and live monitor data
(GPU/CPU/RAM/disk) consumed by the UI's status bar. The UI talks to the
existing REST + WebSocket API (/run, /ws, /health).

Routes added:
  GET  /ui            -> the single-page app (index.html)
  GET  /ui/static/*   -> static assets (css/js)
  GET  /api/monitor   -> system metrics (gpu/cpu/ram/disk)
  WS   /ws            -> live engine events (already in server.py)
"""
from __future__ import annotations

import os
import sys
import platform
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


def _app_base_dir() -> Path:
    """App root: frozen .exe folder, else project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _ui_dir() -> Path:
    """Static UI location.

    When frozen (PyInstaller .exe) we prefer the folder next to the executable
    (``<exe_dir>/yacine/ui/static``) so the UI can be updated without rebuilding
    the binary. Fall back to the embedded ``_MEIPASS`` copy if it isn't present.
    """
    if getattr(sys, "frozen", False):
        local = Path(sys.executable).resolve().parent / "yacine" / "ui" / "static"
        if local.exists():
            return local
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "yacine" / "ui" / "static"
    return Path(__file__).resolve().parents[2] / "yacine" / "ui" / "static"


UI_DIR = _ui_dir()


def _gpu_stats() -> Dict[str, object]:
    """Parse nvidia-smi for GPU usage; empty if no NVIDIA GPU / tool."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return {}
        parts = [p.strip() for p in out.stdout.strip().split(",")]
        if len(parts) >= 5:
            return {
                "name": parts[0],
                "utilization": float(parts[1] or 0),
                "mem_used_mb": float(parts[2] or 0),
                "mem_total_mb": float(parts[3] or 0),
                "temperature_c": float(parts[4] or 0),
            }
    except Exception:
        pass
    return {}


def _system_stats() -> Dict[str, object]:
    """Lightweight system stats without external deps (psutil optional)."""
    import subprocess
    stats: Dict[str, object] = {"platform": platform.system()}
    # CPU percent via a short busy-wait-free probe is not trivial without psutil;
    # fall back to nproc count + load average on POSIX.
    try:
        import os
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        stats["load_avg"] = list(load)
        stats["cpu_count"] = os.cpu_count() or 0
    except Exception:
        pass
    # Memory via cgroup/proc on Linux, or wmic on Windows.
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize",
                 "/Value"], capture_output=True, text=True, timeout=5)
            mem = {}
            for line in out.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    mem[k.strip()] = int(v.strip() or 0)
            total_kb = mem.get("TotalVisibleMemorySize", 0)
            free_kb = mem.get("FreePhysicalMemory", 0)
            used_kb = max(total_kb - free_kb, 0)
            stats["ram_used_gb"] = round(used_kb / 1e6, 1)
            stats["ram_total_gb"] = round(total_kb / 1e6, 1)
            stats["ram_percent"] = round(100 * used_kb / total_kb, 1) if total_kb else 0
        else:
            # Linux: /proc/meminfo
            info = (Path("/proc/meminfo").read_text(errors="ignore")
                    if Path("/proc/meminfo").exists() else "")
            d = {}
            for line in info.splitlines():
                k, _, v = line.partition(":")
                d[k.strip()] = int((v.strip().split()[0] or 0))
            total = d.get("MemTotal", 0)
            avail = d.get("MemAvailable", 0)
            stats["ram_used_gb"] = round((total - avail) / 1e6, 1)
            stats["ram_total_gb"] = round(total / 1e6, 1)
            stats["ram_percent"] = round(100 * (total - avail) / total, 1) if total else 0
    except Exception:
        stats["ram_error"] = "unavailable"
    # Disk
    try:
        total, used, free = shutil.disk_usage(str(Path.home()))
        stats["disk_free_gb"] = round(free / 1e9, 1)
        stats["disk_total_gb"] = round(total / 1e9, 1)
        stats["disk_percent"] = round(100 * used / total, 1)
    except Exception:
        pass
    return stats



router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("", response_class=HTMLResponse)
def ui_index():
    index = UI_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Y.A.C.I.N.E UI</h1><p>index.html missing.</p>", status_code=200)
    return FileResponse(index)


@router.get("/static/{path:path}")
def ui_static(path: str):
    fp = (UI_DIR / path).resolve()
    if not str(fp).startswith(str(UI_DIR.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if fp.exists():
        return FileResponse(fp)
    return JSONResponse({"error": "not found"}, status_code=404)


# Monitor endpoint (registered under /api)
def monitor_endpoint():
    stats: Dict[str, object] = {"timestamp": time.time()}
    try:
        stats["system"] = _system_stats()
    except Exception:
        stats["system"] = {"error": "psutil not installed"}
    stats["gpu"] = _gpu_stats()
    return stats
