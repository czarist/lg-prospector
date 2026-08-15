"""Saúde da máquina e dos serviços que o cockpit mostra."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_CPU_PREV: tuple[int, int] | None = None
_CORE_PREV: dict[str, tuple[int, int]] = {}
_STATIC: dict[str, Any] | None = None
_PCI_GPU = {
    ("0x1002", "0x7340"): "Radeon RX 5500",
    ("0x8086", "0x0412"): "Intel HD 4600",
}


def _sys_read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _sys_int(path: str) -> int | None:
    raw = _sys_read(path)
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _hwmon_dir(name: str) -> str | None:
    root = "/sys/class/hwmon"
    if not os.path.isdir(root):
        return None
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    for ent in entries:
        path = os.path.join(root, ent)
        if _sys_read(os.path.join(path, "name")) == name:
            return path
    return None


def _milli(path: str) -> float | None:
    n = _sys_int(path)
    if n is None:
        return None
    return n / 1000.0


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
    total = avail = swap_total = swap_free = cached = 0
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key = line.split(":")[0]
            try:
                kb = int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                continue
            if key == "MemTotal":
                total = kb
            elif key == "MemAvailable":
                avail = kb
            elif key == "SwapTotal":
                swap_total = kb
            elif key == "SwapFree":
                swap_free = kb
            elif key == "Cached":
                cached = kb
    used = max(0, total - avail)
    pct = (used / total * 100.0) if total else 0.0
    swap_used = max(0, swap_total - swap_free)
    swap_pct = (swap_used / swap_total * 100.0) if swap_total else 0.0
    return {
        "total": total,
        "used": used,
        "avail": avail,
        "pct": pct,
        "cached": cached,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_pct": swap_pct,
    }


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


def _short_cpu_model(raw: str) -> str:
    name = raw.replace("(R)", "").replace("(TM)", "").replace(" CPU ", " ")
    name = name.replace("Processor", "").replace("  ", " ").strip()
    return name


def _cpu_static() -> dict[str, Any]:
    model = ""
    threads = 0
    cores = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                elif line.startswith("processor"):
                    threads += 1
                elif line.startswith("cpu cores") and not cores:
                    try:
                        cores = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        cores = 0
    except OSError:
        pass
    threads = os.cpu_count() or threads or 0
    max_khz = _sys_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    min_khz = _sys_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
    return {
        "model": _short_cpu_model(model) or "CPU",
        "threads": threads,
        "cores": cores or max(1, threads // 2),
        "max_mhz": (max_khz or 0) / 1000.0,
        "min_mhz": (min_khz or 0) / 1000.0,
        "governor": _sys_read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
    }


def _board_static() -> dict[str, Any]:
    vendor = _sys_read("/sys/class/dmi/id/board_vendor") or _sys_read(
        "/sys/class/dmi/id/sys_vendor"
    )
    name = _sys_read("/sys/class/dmi/id/board_name") or _sys_read(
        "/sys/class/dmi/id/product_name"
    )
    return {
        "vendor": vendor,
        "name": name,
        "product": _sys_read("/sys/class/dmi/id/product_name"),
        "bios": _sys_read("/sys/class/dmi/id/bios_version"),
        "bios_date": _sys_read("/sys/class/dmi/id/bios_date"),
        "bios_vendor": _sys_read("/sys/class/dmi/id/bios_vendor"),
    }


def _os_pretty() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _host_static() -> dict[str, Any]:
    uname = os.uname()
    return {
        "hostname": socket.gethostname(),
        "os": _os_pretty() or uname.sysname,
        "kernel": uname.release,
        "arch": uname.machine,
    }


def _lspci_name(vendor: str, device: str) -> str:
    key = (vendor.lower(), device.lower())
    if key in _PCI_GPU:
        return _PCI_GPU[key]
    needle = f"{vendor[2:] if vendor.startswith('0x') else vendor}:{device[2:] if device.startswith('0x') else device}".lower()
    try:
        out = subprocess.check_output(
            ["lspci", "-nn"],
            stderr=subprocess.DEVNULL,
            timeout=1.2,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""
    for line in out.splitlines():
        if needle in line.lower():
            name = line.split(":", 2)[-1]
            name = name.split("[", 1)[0].strip()
            return name
    return ""


def _disk_static() -> dict[str, dict[str, Any]]:
    """mount path → {model, tran, rota, dev}."""
    out: dict[str, dict[str, Any]] = {}
    try:
        raw = subprocess.check_output(
            [
                "lsblk",
                "-J",
                "-b",
                "-o",
                "NAME,TYPE,MOUNTPOINT,MODEL,SIZE,TRAN,ROTA",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            text=True,
        )
        tree = json.loads(raw).get("blockdevices") or []
    except Exception:
        return out

    def walk(nodes: list[Any], model: str, tran: str, rota: bool, disk: str) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type") or ""
            name = str(node.get("name") or "")
            this_model = (node.get("model") or model or "").strip()
            this_tran = (node.get("tran") or tran or "").strip()
            this_rota = bool(node.get("rota")) if node.get("rota") is not None else rota
            this_disk = name if kind == "disk" else disk
            mp = node.get("mountpoint")
            if mp in {"/", "/ssd", "/storage"}:
                out[mp] = {
                    "model": this_model,
                    "tran": this_tran,
                    "rota": this_rota,
                    "dev": this_disk or name,
                    "kind": "hdd" if this_rota else "ssd",
                }
            kids = node.get("children") or []
            if kids:
                walk(kids, this_model, this_tran, this_rota, this_disk)

    walk(tree, "", "", False, "")
    return out


def static_hw() -> dict[str, Any]:
    global _STATIC
    if _STATIC is None:
        _STATIC = {
            "cpu": _cpu_static(),
            "board": _board_static(),
            "host": _host_static(),
            "disks": _disk_static(),
        }
    return _STATIC


def cpu_freq() -> dict[str, Any]:
    freqs: list[float] = []
    n = os.cpu_count() or 0
    for i in range(n):
        khz = _sys_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
        if khz:
            freqs.append(khz / 1000.0)
    if not freqs:
        return {}
    return {
        "avg_mhz": sum(freqs) / len(freqs),
        "min_mhz": min(freqs),
        "max_mhz": max(freqs),
        "n": len(freqs),
    }


def cpu_core_usage() -> list[float]:
    usages: list[float] = []
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return usages
    for line in lines:
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        if len(line) < 4 or not line[3].isdigit():
            continue
        parts = line.split()
        name = parts[0]
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        prev = _CORE_PREV.get(name)
        _CORE_PREV[name] = (total, idle)
        if prev is None:
            usages.append(0.0)
            continue
        dt = total - prev[0]
        di = idle - prev[1]
        if dt <= 0:
            usages.append(0.0)
        else:
            usages.append(max(0.0, min(100.0, 100.0 * (1.0 - di / dt))))
    return usages


def cpu_temps() -> dict[str, Any]:
    hw = _hwmon_dir("coretemp")
    if not hw:
        pkg = _milli("/sys/class/thermal/thermal_zone3/temp")
        return {"package": pkg, "cores": [], "crit": None} if pkg else {}
    package = None
    crit = None
    cores: list[float] = []
    for ent in sorted(os.listdir(hw)):
        if not ent.startswith("temp") or not ent.endswith("_input"):
            continue
        base = ent[: -len("_input")]
        label = _sys_read(os.path.join(hw, base + "_label")).lower()
        val = _milli(os.path.join(hw, ent))
        if val is None:
            continue
        if "package" in label:
            package = val
            crit = _milli(os.path.join(hw, base + "_crit"))
        elif "core" in label:
            cores.append(val)
    if package is None and cores:
        package = max(cores)
    return {"package": package, "cores": cores, "crit": crit, "hot": max(cores) if cores else package}


def uptime_seconds() -> float:
    raw = _sys_read("/proc/uptime")
    if not raw:
        return 0.0
    try:
        return float(raw.split()[0])
    except ValueError:
        return 0.0


def _nvidia_gpu() -> dict[str, Any] | None:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,clocks.sm,clocks.mem,power.draw,fan.speed",
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

    def _f(idx: int) -> float | None:
        if idx >= len(parts):
            return None
        try:
            return float(parts[idx])
        except ValueError:
            return None

    try:
        util = float(parts[1])
        mem_used = float(parts[2]) * 1024 * 1024
        mem_total = float(parts[3]) * 1024 * 1024
    except ValueError:
        return None
    return {
        "name": parts[0],
        "vendor": "NVIDIA",
        "util": util,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_pct": (mem_used / mem_total * 100.0) if mem_total else 0.0,
        "temp": _f(4),
        "sclk_mhz": _f(5),
        "mclk_mhz": _f(6),
        "power_w": _f(7),
        "fan_rpm": None,
        "fan_pct": _f(8),
    }


def _hwmon_under(dev: str) -> str | None:
    hw_root = os.path.join(dev, "hwmon")
    if not os.path.isdir(hw_root):
        return None
    try:
        kids = sorted(os.listdir(hw_root))
    except OSError:
        return None
    for kid in kids:
        path = os.path.join(hw_root, kid)
        if os.path.isdir(path):
            return path
    return None


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
        vendor = _sys_read(os.path.join(dev, "vendor"))
        if vendor not in {"0x1002", "0x1022"}:
            continue
        try:
            util = float(_sys_read(busy_p) or "0")
            used = int(_sys_read(os.path.join(dev, "mem_info_vram_used")) or "0")
            total = int(_sys_read(os.path.join(dev, "mem_info_vram_total")) or "0")
        except ValueError:
            continue
        device = _sys_read(os.path.join(dev, "device"))
        hw = _hwmon_under(dev)
        sclk = mclk = power = fan = temp = None
        power_cap = None
        if hw:
            temp = _milli(os.path.join(hw, "temp1_input"))
            sclk_hz = _sys_int(os.path.join(hw, "freq1_input"))
            mclk_hz = _sys_int(os.path.join(hw, "freq2_input"))
            if sclk_hz:
                sclk = sclk_hz / 1_000_000.0
            if mclk_hz:
                mclk = mclk_hz / 1_000_000.0
            p_uw = _sys_int(os.path.join(hw, "power1_average"))
            cap_uw = _sys_int(os.path.join(hw, "power1_cap"))
            if p_uw is not None:
                power = p_uw / 1_000_000.0
            if cap_uw is not None:
                power_cap = cap_uw / 1_000_000.0
            fan_n = _sys_int(os.path.join(hw, "fan1_input"))
            if fan_n is not None:
                fan = fan_n
        link = _sys_read(os.path.join(dev, "current_link_speed"))
        width = _sys_read(os.path.join(dev, "current_link_width"))
        return {
            "name": _lspci_name(vendor, device) or "Radeon",
            "vendor": "AMD",
            "util": util,
            "mem_used": used,
            "mem_total": total,
            "mem_pct": (used / total * 100.0) if total else 0.0,
            "temp": temp,
            "sclk_mhz": sclk,
            "mclk_mhz": mclk,
            "power_w": power,
            "power_cap_w": power_cap,
            "fan_rpm": fan,
            "pcie": f"{link} x{width}".strip() if link or width else "",
        }
    return None


def _intel_igpu() -> dict[str, Any] | None:
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return None
    for name in sorted(os.listdir(drm)):
        if not name.startswith("card") or not name[4:].isdigit():
            continue
        dev = os.path.join(drm, name, "device")
        vendor = _sys_read(os.path.join(dev, "vendor"))
        if vendor != "0x8086":
            continue
        device = _sys_read(os.path.join(dev, "device"))
        return {"name": _lspci_name(vendor, device) or "Intel iGPU", "vendor": "Intel"}
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
    hw = static_hw()
    disks: dict[str, Any] = {}
    meta = hw.get("disks") or {}
    for key, path in (("principal", "/"), ("ssd", "/ssd"), ("hd", "/storage")):
        info = disk_info(path)
        if not info:
            continue
        extra = meta.get(path) or {}
        info.update(extra)
        disks[key] = info
    cores = cpu_core_usage()
    temps = cpu_temps()
    return {
        "cpu": cpu_percent(),
        "cpu_info": hw.get("cpu") or {},
        "cpu_freq": cpu_freq(),
        "cpu_temp": temps,
        "cpu_cores": cores,
        "cpu_active": sum(1 for u in cores if u >= 8.0),
        "ram": ram_info(),
        "gpu": gpu_info(),
        "igpu": _intel_igpu(),
        "board": hw.get("board") or {},
        "host": {**(hw.get("host") or {}), "uptime": uptime_seconds()},
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
