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

import json
import sys
import platform
import shutil
import time
from pathlib import Path
from typing import Dict

from fastapi import APIRouter
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


# nvidia-smi costs 100-500ms per call, and considerably more while the GPU is
# busy generating — which is exactly when the UI polls hardest. Remember when
# there is no NVIDIA GPU at all so we stop paying for the probe forever.
_NO_GPU = False


def _gpu_stats() -> Dict[str, object]:
    """Parse nvidia-smi for GPU usage; empty if no NVIDIA GPU / tool."""
    global _NO_GPU
    if _NO_GPU:
        return {}
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            _NO_GPU = True
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
    except FileNotFoundError:
        _NO_GPU = True          # nvidia-smi is not installed; never retry
    except Exception:
        pass
    return {}


def _system_stats() -> Dict[str, object]:
    """Lightweight system stats without external deps (psutil optional)."""
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
            # GlobalMemoryStatusEx via ctypes — microseconds, no process spawn.
            # `wmic` was costing 0.5-2 SECONDS per call here, every poll, which
            # is a large part of why the UI felt like it was dragging.
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            total = int(m.ullTotalPhys)
            used = total - int(m.ullAvailPhys)
            stats["ram_used_gb"] = round(used / 1e9, 1)
            stats["ram_total_gb"] = round(total / 1e9, 1)
            stats["ram_percent"] = round(100 * used / total, 1) if total else 0
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


# Injected into the page so the UI can authenticate against its own server.
#
# The API requires a key on every call. The UI is served by that same process,
# so it is handed the key here and adds it to every same-origin request. This
# is safe: CORS is pinned to this server's origin and the Host header is
# checked, so a page on another site cannot READ this HTML to steal the key —
# it can only send requests, which are rejected without one.
_AUTH_SHIM = """<script>
window.YACINE_API_KEY = %(key)s;
(function () {
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input : (input && input.url) || "";
    var sameOrigin = url.startsWith("/") ||
                     url.startsWith(window.location.origin);
    if (sameOrigin) {
      init = init || {};
      var h = new Headers(init.headers || (typeof input === "object" && input.headers) || {});
      if (!h.has("X-API-Key")) h.set("X-API-Key", window.YACINE_API_KEY);
      init.headers = h;
    }
    return realFetch(input, init);
  };
  // WebSockets cannot carry headers, so the key rides in the query string.
  window.yacineWsUrl = function (path) {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host + path +
           "?api_key=" + encodeURIComponent(window.YACINE_API_KEY);
  };
})();
</script>"""


@router.get("", response_class=HTMLResponse)
def ui_index():
    index = UI_DIR / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>Y.A.C.I.N.E UI</h1><p>index.html was not found at "
            f"<code>{UI_DIR}</code>.</p>", status_code=200)
    from ..core.auth import get_api_key
    html = index.read_text(encoding="utf-8", errors="ignore")
    shim = _AUTH_SHIM % {"key": json.dumps(get_api_key())}
    if "</head>" in html:
        html = html.replace("</head>", shim + "\n</head>", 1)
    else:
        html = shim + html
    return HTMLResponse(html)


@router.get("/static/{path:path}")
def ui_static(path: str):
    fp = (UI_DIR / path).resolve()
    if not str(fp).startswith(str(UI_DIR.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if fp.exists():
        return FileResponse(fp)
    return JSONResponse({"error": "not found"}, status_code=404)


# Monitor endpoint (registered under /api)
# The UI polls this on a timer, so the answer is cached: hardware stats do not
# change meaningfully between polls, and recomputing them mid-generation steals
# time from the thing the user is actually waiting for.
_MONITOR_TTL = 2.5
_monitor_cache: Dict[str, object] = {}
_monitor_at = 0.0


def monitor_endpoint():
    global _monitor_cache, _monitor_at
    now = time.time()
    if _monitor_cache and (now - _monitor_at) < _MONITOR_TTL:
        return _monitor_cache
    stats: Dict[str, object] = {"timestamp": now}
    try:
        stats["system"] = _system_stats()
    except Exception as e:  # noqa: BLE001
        stats["system"] = {"error": f"unavailable: {type(e).__name__}"}
    stats["gpu"] = _gpu_stats()
    _monitor_cache, _monitor_at = stats, now
    return stats
