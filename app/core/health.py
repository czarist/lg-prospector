"""Saúde da máquina e dos serviços que o cockpit mostra."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

_CPU_PREV: tuple[int, int] | None = None


def _cpu_sample() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_percent() -> float | None:
    global _CPU_PREV
    now = _cpu_sample()
    prev = _CPU_PREV
    _CPU_PREV = now
    if prev is None:
        return None
    dt = now[0] - prev[0]
    di = now[1] - prev[1]
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))


def ram_info() -> dict[str, Any]:
    total = avail = 0
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    used = max(0, total - avail)
    pct = (used / total * 100.0) if total else 0.0
    return {"total": total, "used": used, "avail": avail, "pct": pct}


def disk_info(path: str) -> dict[str, Any] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = max(0, total - free)
    pct = (used / total * 100.0) if total else 0.0
    return {"path": path, "total": total, "used": used, "free": free, "pct": pct}


def _nvidia_gpu() -> dict[str, Any] | None:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    line = (out or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 4:
        return None
    try:
        util = float(parts[1])
        mem_used = float(parts[2]) * 1024 * 1024
        mem_total = float(parts[3]) * 1024 * 1024
    except ValueError:
        return None
    return {
        "name": parts[0],
        "util": util,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_pct": (mem_used / mem_total * 100.0) if mem_total else 0.0,
    }


def _amd_gpu() -> dict[str, Any] | None:
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return None
    for name in sorted(os.listdir(drm)):
        if not name.startswith("card") or not name[4:].isdigit():
            continue
        dev = os.path.join(drm, name, "device")
        busy_p = os.path.join(dev, "gpu_busy_percent")
        if not os.path.isfile(busy_p):
            continue
        try:
            vendor = open(os.path.join(dev, "vendor"), encoding="utf-8").read().strip()
        except OSError:
            vendor = ""
        if vendor not in {"0x1002", "0x1022"}:
            continue
        try:
            util = float(open(busy_p, encoding="utf-8").read().strip())
            used = int(open(os.path.join(dev, "mem_info_vram_used"), encoding="utf-8").read())
            total = int(open(os.path.join(dev, "mem_info_vram_total"), encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        return {
            "name": "Radeon",
            "util": util,
            "mem_used": used,
            "mem_total": total,
            "mem_pct": (used / total * 100.0) if total else 0.0,
        }
    return None


def gpu_info() -> dict[str, Any] | None:
    return _nvidia_gpu() or _amd_gpu()


def loadavg() -> tuple[float, float, float]:
    try:
        return os.getloadavg()
    except OSError:
        return (0.0, 0.0, 0.0)


def _tcp_ok(host: str, port: int, timeout: float = 1.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def mysql_status() -> dict[str, Any]:
    from app.core.config import get_settings

    s = get_settings()
    host, port = s.mysql_host, int(s.mysql_port)
    if not _tcp_ok(host, port):
        return {"ok": False, "label": "mysql", "detail": "fechado"}
    try:
        import asyncio

        import aiomysql

        async def _ping() -> None:
            conn = await aiomysql.connect(
                host=host,
                port=port,
                user=s.mysql_user,
                password=s.mysql_password,
                db=s.mysql_database,
                connect_timeout=2,
                autocommit=True,
            )
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()
            finally:
                conn.close()

        asyncio.run(_ping())
        return {"ok": True, "label": "mysql", "detail": s.mysql_database}
    except Exception as exc:
        return {"ok": False, "label": "mysql", "detail": str(exc)[:40]}


def redis_status() -> dict[str, Any]:
    from app.core.config import get_settings

    url = get_settings().redis_url
    try:
        import redis

        r = redis.from_url(url, socket_connect_timeout=1.2, socket_timeout=1.2)
        r.ping()
        return {"ok": True, "label": "redis", "detail": "pong"}
    except Exception as exc:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        if _tcp_ok(host, port):
            return {"ok": False, "label": "redis", "detail": str(exc)[:40]}
        return {"ok": False, "label": "redis", "detail": "fechado"}


def http_status(url: str, label: str) -> dict[str, Any]:
    if not url.startswith("http"):
        url = "https://" + url
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "lg-cockpit/1.0", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(code) < 400
            return {"ok": ok, "label": label, "detail": str(code), "url": url}
    except urllib.error.HTTPError as exc:
        ok = 200 <= int(exc.code) < 500
        return {"ok": ok, "label": label, "detail": str(exc.code), "url": url}
    except Exception as exc:
        return {"ok": False, "label": label, "detail": str(exc)[:32], "url": url}


def litellm_status() -> dict[str, Any]:
    from app.core.config import get_settings

    base = (get_settings().base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "label": "qwen", "detail": "sem url"}
    # /v1/models é o ping mais estável do LiteLLM
    url = base if base.endswith("/models") else base.rstrip("/") + "/models"
    st = http_status(url, "qwen")
    st["label"] = "qwen"
    return st


def collect_machine() -> dict[str, Any]:
    disks: dict[str, Any] = {}
    for key, path in (("principal", "/"), ("ssd", "/ssd"), ("hd", "/storage")):
        info = disk_info(path)
        if info:
            disks[key] = info
    return {
        "cpu": cpu_percent(),
        "ram": ram_info(),
        "gpu": gpu_info(),
        "disks": disks,
        "load": loadavg(),
        "ts": time.time(),
    }


def collect_services() -> dict[str, Any]:
    from app.core.config import get_settings

    crm = get_settings().effective_crm_url or "https://trentincrm.com.br"
    crm_origin = f"{urlparse(crm).scheme}://{urlparse(crm).netloc}" if "://" in crm else "https://trentincrm.com.br"
    return {
        "mysql": mysql_status(),
        "redis": redis_status(),
        "qwen": litellm_status(),
        "crm": http_status(crm_origin, "crm"),
        "site": http_status("https://trentin.software", "site"),
        "ts": time.time(),
    }


class HealthWatch:
    """Atualiza CPU/RAM a cada 1s e MySQL/Redis/HTTP a cada 8s, fora da UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._data: dict[str, Any] = {"machine": {}, "services": {}}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cockpit-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "machine": dict(self._data.get("machine") or {}),
                "services": dict(self._data.get("services") or {}),
            }

    def _run(self) -> None:
        last_svc = 0.0
        while not self._stop.is_set():
            try:
                machine = collect_machine()
                now = time.time()
                services = None
                if now - last_svc >= 8:
                    services = collect_services()
                    last_svc = now
                with self._lock:
                    self._data["machine"] = machine
                    if services is not None:
                        self._data["services"] = services
            except Exception:
                pass
            self._stop.wait(1.2)
