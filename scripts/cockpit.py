#!/usr/bin/env python3
"""Sobe nicho + generalista + mailman e abre o painel.

  ./cockpit          # sobe o que faltar + painel
  ./cockpit --attach # só o painel
  ./cockpit --stop   # encerra os três

q fecha o painel. Os processos continuam. --stop derruba.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import __version__
from app.core.health import HealthWatch
from app.core.live import read_live
from app.core.paths import logs_dir
from app.domain.cities import DEFAULT_NICHES, build_city_queue


def _session_path() -> Path:
    return logs_dir() / "cockpit.json"


def _hunt_results() -> Path:
    return logs_dir() / "hunt"


def _gen_jsonl() -> Path:
    return logs_dir() / "generalista.jsonl"

SCRIPTS = {
    "nicho": ROOT / "scripts" / "hunt_loop.py",
    "generalista": ROOT / "scripts" / "hunt_generalista.py",
    "mailman": ROOT / "scripts" / "mailman.py",
}
def _console_logs() -> dict[str, Path]:
    root = logs_dir()
    return {
        "nicho": root / "hunt" / "console_loop.log",
        "generalista": root / "hunt" / "console_generalista.log",
        "mailman": root / "mailman" / "console.log",
    }
CMDS: dict[str, list[str]] = {
    "nicho": [
        "--focus-rs", "--max-tier", "3", "--min-pop-k", "50", "-n", "10",
        "--stages", "discover,enrich,crm",
        "--pause", "8", "--cycle-pause", "60", "--max-partial-attempts", "5",
    ],
    "generalista": [
        "--focus-rs", "--max-tier", "3", "--min-pop-k", "50", "-n", "8",
        "--pause", "8", "--cycle-pause", "60",
    ],
    "mailman": [
        "--batch-size", "2", "--min-interval", "120", "--max-interval", "300",
        "--cooldown-days", "4", "--wait-days", "4",
    ],
}

_AVG_HUNT_JOB = 420.0
_AVG_GEN_CITY = 360.0
_AVG_MAIL_GAP = 210.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_stop = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} min {s:02d}s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}"


def _fmt_bytes(n: float | int | None) -> str:
    if not n:
        return "0"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            if unit in {"B", "K"}:
                return f"{int(n)}{unit}"
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def _fmt_clock(seconds: float | None) -> str:
    """Tempo corrido mm:ss — sempre anda, nunca congela em 0."""
    if seconds is None or seconds < 0:
        return "0:00"
    sec = int(seconds)
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v and v > 0)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return float(clean[mid])
    return (clean[mid - 1] + clean[mid]) / 2


def _tail_jsonl(path: Path, limit: int = 80) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()[-120_000:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines()[-limit:]:
        try:
            row = json.loads(line.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _hunt_avg() -> float:
    day = datetime.now().strftime("%Y%m%d")
    rows = _tail_jsonl(_hunt_results() / f"results_{day}.jsonl", 120)
    elapsed = [
        float(r["elapsed_s"])
        for r in rows
        if r.get("event") == "job_done" and r.get("elapsed_s")
    ]
    return _median(elapsed) or _AVG_HUNT_JOB


def _gen_avg() -> float:
    stamps = [
        ts
        for r in _tail_jsonl(_gen_jsonl(), 40)
        if r.get("event") == "cycle"
        for ts in [_parse_ts(r.get("ts"))]
        if ts
    ]
    diffs = [
        (stamps[i] - stamps[i - 1]).total_seconds()
        for i in range(1, len(stamps))
        if 30 < (stamps[i] - stamps[i - 1]).total_seconds() < 3600
    ]
    return _median(diffs) or _AVG_GEN_CITY


def _queue_totals() -> tuple[int, int]:
    cities = build_city_queue(focus_rs=True, max_tier=3, min_population_k=50)
    return len(cities) * max(1, len(DEFAULT_NICHES)), len(cities)


def _last_log_line(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        raw = path.read_bytes()[-8000:]
    except OSError:
        return ""
    text = _ANSI_RE.sub("", raw.decode("utf-8", errors="ignore"))
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("──") or set(line) <= {"─", " "}:
            continue
        if line.startswith("{"):
            continue
        return line[:110]
    return ""


def _log_age(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _today_hunt_stats() -> dict[str, Any]:
    day = datetime.now().strftime("%Y%m%d")
    rows = _tail_jsonl(_hunt_results() / f"results_{day}.jsonl", 200)
    jobs = [r for r in rows if r.get("event") == "job_done"]
    leads = sum(int(r.get("good") or 0) for r in jobs)
    return {"jobs": len(jobs), "leads": leads}


def _today_mail_stats() -> dict[str, Any]:
    live = read_live("mailman")
    return {
        "sent": int(live.get("sent_total") or 0),
        "failed": int(live.get("failed_total") or 0),
        "batch": live.get("batch"),
    }


def _pipeline(phase: str, stages: tuple[str, ...] = ("discover", "enrich", "crm")) -> str:
    cur = (phase or "").lower()
    parts: list[str] = []
    seen = False
    for st in stages:
        if st == cur:
            parts.append(f"▸{st}")
            seen = True
        elif not seen:
            parts.append(f"✓{st}")
        else:
            parts.append(st)
    if cur in {"job", "idle", "erro", "descoberta", "lote", "pausa", "fila_vazia"}:
        return cur
    return "  ".join(parts)


def find_pids(script_name: str) -> list[int]:
    needle = f"scripts/{script_name}"
    found: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    me = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode()
        except OSError:
            continue
        if needle in cmd and "cockpit.py" not in cmd:
            found.append(pid)
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_worker(name: str) -> tuple[int, str]:
    existing = [p for p in find_pids(SCRIPTS[name].name) if _alive(p)]
    if existing:
        return existing[0], "já rodava"
    log_path = _console_logs()[name]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_fh.write(f"\n── cockpit {_now().isoformat()} ──\n")
    log_fh.flush()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS[name]), *CMDS[name]],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid, "subiu"


def stop_all() -> None:
    for name in SCRIPTS:
        for pid in find_pids(SCRIPTS[name].name):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.time() + 25
    while time.time() < deadline:
        left = [
            pid
            for name in SCRIPTS
            for pid in find_pids(SCRIPTS[name].name)
            if _alive(pid)
        ]
        if not left:
            return
        time.sleep(0.4)
    for name in SCRIPTS:
        for pid in find_pids(SCRIPTS[name].name):
            if _alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def _bar(pct: float, width: int = 22) -> str:
    pct = max(0.0, min(100.0, pct))
    fill = min(width, max(0, int(round(width * pct / 100.0))))
    return "█" * fill + "░" * (width - fill)


def _put(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0:
        return
    try:
        stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
    except curses.error:
        pass


def _snapshot() -> dict[str, Any]:
    hunt_jobs, gen_cities = _queue_totals()
    hunt_avg = _hunt_avg()
    gen_avg = _gen_avg()
    now = _now()
    workers: dict[str, dict[str, Any]] = {}

    for name, script in SCRIPTS.items():
        pids = [p for p in find_pids(script.name) if _alive(p)]
        live = read_live(name)
        running = bool(pids)
        log_path = _console_logs()[name]
        log_age = _log_age(log_path)
        last_log = _last_log_line(log_path)
        live_ts = _parse_ts(live.get("ts"))
        pulse = (now - live_ts).total_seconds() if live_ts else None
        stale = bool(running and log_age is not None and log_age > 120)
        overtime = False
        row: dict[str, Any] = {
            "running": running,
            "pid": pids[0] if pids else None,
            "stale": stale,
            "last_log": last_log,
            "log_age": log_age,
            "pulse": pulse,
        }
        if name == "nicho":
            idx = int(live.get("index") or 0)
            total = int(live.get("total") or hunt_jobs) or hunt_jobs
            started = _parse_ts(live.get("job_started_at"))
            spent = (now - started).total_seconds() if started and running else 0.0
            overtime = bool(running and spent > hunt_avg)
            remain = max(0.0, hunt_avg - spent) if running and not overtime else None
            rest = max(0, total - idx) if idx else max(0, total - 1)
            cycle_eta = ((remain if remain is not None else 0) + rest * (hunt_avg + 8)) if running else None
            label = "aguardando job"
            if live.get("niche") or live.get("city"):
                label = f"{live.get('niche') or '—'}  ·  {live.get('city') or '—'}"
            last = ""
            if live.get("last_good") is not None:
                last = f"último job +{live.get('last_good')} leads em {_fmt_dur(live.get('last_elapsed_s'))}"
            row.update(
                {
                    "title": "NICHO",
                    "label": label,
                    "phase": live.get("phase") or ("rodando" if running else "parado"),
                    "pipeline": _pipeline(str(live.get("phase") or "")),
                    "progress": f"{idx}/{total}",
                    "cycle": live.get("cycle"),
                    "pct": (max(idx - 1, 0) / total * 100) if total else 0.0,
                    "spent": spent if running else None,
                    "remain": remain,
                    "overtime": overtime,
                    "eta_cycle": cycle_eta,
                    "avg": hunt_avg,
                    "detail": live.get("detail") or "",
                    "ok": live.get("ok"),
                    "err": live.get("err"),
                    "last": last,
                }
            )
        elif name == "generalista":
            idx = int(live.get("index") or 0)
            total = int(live.get("total") or gen_cities) or gen_cities
            started = _parse_ts(live.get("job_started_at"))
            spent = (now - started).total_seconds() if started and running else 0.0
            overtime = bool(running and spent > gen_avg)
            remain = max(0.0, gen_avg - spent) if running and not overtime else None
            rest = max(0, total - idx) if idx else max(0, total - 1)
            cycle_eta = ((remain if remain is not None else 0) + rest * (gen_avg + 8)) if running else None
            last = ""
            if live.get("last_new") is not None:
                last = (
                    f"última cidade {live.get('last_city') or '—'}  "
                    f"achou {live.get('last_found') or 0}  "
                    f"crm +{live.get('last_new')}"
                )
            row.update(
                {
                    "title": "GENERALISTA",
                    "label": live.get("city") or "aguardando cidade",
                    "phase": live.get("phase") or ("rodando" if running else "parado"),
                    "pipeline": str(live.get("phase") or ""),
                    "progress": f"{idx}/{total}",
                    "cycle": live.get("cycle"),
                    "pct": (max(idx - 1, 0) / total * 100) if total else 0.0,
                    "spent": spent if running else None,
                    "remain": remain,
                    "overtime": overtime,
                    "eta_cycle": cycle_eta,
                    "avg": gen_avg,
                    "detail": live.get("detail") or "",
                    "last": last,
                }
            )
        else:
            next_at = _parse_ts(live.get("next_at"))
            until = (next_at - now).total_seconds() if next_at else None
            if until is not None:
                until = max(0.0, until)
            row.update(
                {
                    "title": "MAILMAN",
                    "label": live.get("last_line") or "avaliando fila",
                    "phase": live.get("phase") or ("rodando" if running else "parado"),
                    "pipeline": "1 nicho + 1 generalista",
                    "progress": f"lote {live.get('batch')}" if live.get("batch") else "lote —",
                    "cycle": None,
                    "pct": None,
                    "spent": None,
                    "remain": until,
                    "overtime": False,
                    "eta_cycle": None,
                    "avg": _AVG_MAIL_GAP,
                    "detail": f"fila {live.get('candidates') if live.get('candidates') is not None else '—'}",
                    "last": (
                        f"enviados {live.get('sent_total') or 0}  "
                        f"falha {live.get('failed_total') or 0}  "
                        f"pausa 2–5 min"
                    ),
                }
            )
        workers[name] = row

    hunt = _today_hunt_stats()
    mail = _today_mail_stats()
    return {
        "workers": workers,
        "today_jobs": hunt["jobs"],
        "today_leads": hunt["leads"],
        "mail_sent": mail["sent"],
    }


def _hline(width: int) -> str:
    return "─" * max(0, width)


def _svc_mark(svc: dict[str, Any] | None) -> str:
    if not svc:
        return "?"
    return "●" if svc.get("ok") else "○"


def _disk_bit(label: str, info: dict[str, Any] | None) -> str:
    if not info:
        return f"{label} —"
    return (
        f"{label} {info['pct']:.0f}% "
        f"{_fmt_bytes(info['used'])}/{_fmt_bytes(info['total'])}"
    )


def _machine_line(machine: dict[str, Any]) -> str:
    cpu = machine.get("cpu")
    ram = machine.get("ram") or {}
    gpu = machine.get("gpu")
    load = machine.get("load") or (0, 0, 0)
    bits = []
    bits.append(f"cpu {cpu:.0f}%" if cpu is not None else "cpu …")
    if ram.get("total"):
        bits.append(f"ram {_fmt_bytes(ram.get('used'))}/{_fmt_bytes(ram.get('total'))} ({ram.get('pct', 0):.0f}%)")
    if gpu:
        bits.append(f"gpu {gpu.get('util', 0):.0f}%  vram {_fmt_bytes(gpu.get('mem_used'))}/{_fmt_bytes(gpu.get('mem_total'))}")
    else:
        bits.append("gpu 0%")
    bits.append(f"load {load[0]:.1f}")
    return "   ".join(bits)


def _disks_line(machine: dict[str, Any]) -> str:
    disks = machine.get("disks") or {}
    return "   ".join(
        [
            _disk_bit("principal", disks.get("principal")),
            _disk_bit("ssd", disks.get("ssd")),
            _disk_bit("hd", disks.get("hd")),
        ]
    )


def _services_line(services: dict[str, Any]) -> str:
    order = ("mysql", "redis", "qwen", "crm", "site")
    parts = []
    for key in order:
        svc = services.get(key) or {}
        mark = _svc_mark(svc)
        detail = svc.get("detail") or ""
        if svc.get("ok"):
            parts.append(f"{mark} {key}")
        else:
            extra = f" {detail}" if detail else ""
            parts.append(f"{mark} {key}{extra}")
    return "   ".join(parts)


def _draw_infra(stdscr: Any, y: int, w: int, health: dict[str, Any]) -> int:
    machine = health.get("machine") or {}
    services = health.get("services") or {}
    inner = max(20, w - 4)
    cpu = machine.get("cpu")
    ram_pct = (machine.get("ram") or {}).get("pct") or 0
    disk_pct = ((machine.get("disks") or {}).get("root") or {}).get("pct") or 0
    hot = (cpu or 0) >= 85 or ram_pct >= 85 or disk_pct >= 90
    color = curses.color_pair(5) if hot else curses.color_pair(4)
    line1 = _machine_line(machine)
    _put(stdscr, y, 1, "┌─ MÁQUINA ", color | curses.A_BOLD)
    rest = inner - 10
    if rest > 2:
        _put(stdscr, y, 12, _hline(rest) + "┐", color)
    y += 1
    _put(stdscr, y, 1, "│", color)
    _put(stdscr, y, 3, line1[: inner - 2], color)
    y += 1
    _put(stdscr, y, 1, "│", color)
    _put(stdscr, y, 3, _disks_line(machine)[: inner - 2], color)
    y += 1
    _put(stdscr, y, 1, "│", color)
    # pinta cada serviço
    x = 3
    order = ("mysql", "redis", "qwen", "crm", "site")
    for key in order:
        svc = services.get(key) or {}
        mark = _svc_mark(svc)
        chunk = f"{mark} {key}"
        if not svc.get("ok") and svc.get("detail"):
            chunk += f" {svc['detail']}"
        sc = curses.color_pair(2) if svc.get("ok") else curses.color_pair(3)
        _put(stdscr, y, x, chunk, sc | curses.A_BOLD)
        x += len(chunk) + 3
    y += 1
    _put(stdscr, y, 1, "└" + _hline(inner) + "┘", color)
    return y + 1


def _draw_card(stdscr: Any, y: int, w: int, row: dict[str, Any]) -> int:
    inner = max(20, w - 4)
    alive = row["running"]
    overtime = bool(row.get("overtime"))
    stale = bool(row.get("stale"))
    if not alive:
        color = curses.color_pair(3)
    elif stale or overtime:
        color = curses.color_pair(5)
    else:
        color = curses.color_pair(2)

    title = row["title"]
    phase = row.get("phase") or ""
    prog = row.get("progress") or ""
    cycle = row.get("cycle")
    pid = row.get("pid")
    head_bits = [f"{'●' if alive else '○'}  {title}"]
    if cycle is not None:
        head_bits.append(f"ciclo {cycle}")
    head_bits.append(prog)
    if pid:
        head_bits.append(f"pid {pid}")
    head = "   ".join(head_bits)
    _put(stdscr, y, 1, f"┌─ {head} ", color | curses.A_BOLD)
    rest = inner - len(head) - 2
    if rest > 2:
        _put(stdscr, y, 4 + len(head), _hline(rest) + "┐", color)
    y += 1

    _put(stdscr, y, 1, "│", color)
    _put(stdscr, y, 3, str(row.get("label") or ""), curses.A_BOLD)
    if phase:
        _put(stdscr, y, min(w - 22, 44), str(row.get("pipeline") or phase), curses.color_pair(4))
    y += 1

    _put(stdscr, y, 1, "│", color)
    if row.get("pct") is not None:
        spent = row.get("spent")
        clock = _fmt_clock(spent) if spent is not None else "—"
        if overtime:
            over = (spent or 0) - float(row.get("avg") or 0)
            timing = f"neste {clock}  +{_fmt_dur(over)} da média"
        elif alive:
            timing = f"neste {clock}  faltam {_fmt_dur(row.get('remain'))}"
        else:
            timing = "parado"
        bar_w = min(18, max(8, w - 56))
        volta = f"  volta {_fmt_dur(row.get('eta_cycle'))}" if alive else ""
        line = f"{timing}  {_bar(float(row['pct']), bar_w)} {float(row['pct']):4.1f}%{volta}"
        _put(stdscr, y, 3, line, curses.color_pair(5) if overtime else curses.color_pair(4))
    else:
        if alive and row.get("remain") is not None:
            timing = f"próximo lote em {_fmt_dur(row.get('remain'))}   {row.get('detail') or ''}"
        else:
            timing = "parado"
        _put(stdscr, y, 3, timing, curses.color_pair(4))
    y += 1

    _put(stdscr, y, 1, "│", color)
    mid = row.get("detail") or ""
    if row.get("last"):
        mid = f"{mid}   {row['last']}".strip()
    extra = []
    if row.get("ok") is not None:
        extra.append(f"ok {row['ok']}")
    if row.get("err"):
        extra.append(f"err {row['err']}")
    if extra:
        mid = (mid + "   " if mid else "") + "  ".join(extra)
    _put(stdscr, y, 3, mid[: inner - 2], curses.A_DIM)
    y += 1

    _put(stdscr, y, 1, "│", color)
    age = row.get("log_age")
    pulse = "log agora" if age is not None and age < 8 else f"log {_fmt_dur(age)}" if age is not None else "sem log"
    if stale:
        pulse = f"sem atividade {_fmt_dur(age)}"
    logline = row.get("last_log") or ""
    _put(stdscr, y, 3, f"{pulse}   {logline}"[: inner - 2], curses.A_DIM)
    y += 1

    _put(stdscr, y, 1, "└" + _hline(inner) + "┘", color)
    return y + 1  # próximo card colado — 24 linhas cabem os 3


def _draw(stdscr: Any, boot_at: float) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(400)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)

    watch = HealthWatch()
    watch.start()
    try:
        while not _stop:
            snap = _snapshot()
            health = watch.snapshot()
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            up = _fmt_dur(time.time() - boot_at)
            clock = datetime.now().strftime("%H:%M:%S")
            alive_n = sum(1 for r in snap["workers"].values() if r["running"])
            _put(
                stdscr,
                0,
                1,
                f" LG PROSPECTOR  v{__version__}   {clock}   up {up}   {alive_n}/3 no ar",
                curses.A_BOLD,
            )
            _put(stdscr, 0, max(1, w - 20), "q fecha  --stop mata", curses.A_DIM)
            _put(
                stdscr,
                1,
                1,
                f" hoje  {snap['today_jobs']} jobs nicho   +{snap['today_leads']} leads   "
                f"{snap['mail_sent']} e-mails mailman",
                curses.color_pair(4),
            )

            y = 3
            if h >= 28:
                y = _draw_infra(stdscr, y, w, health)

            for key in ("nicho", "generalista", "mailman"):
                if y + 6 > h - 1:
                    break
                y = _draw_card(stdscr, y, w, snap["workers"][key])

            if h < 28:
                m = health.get("machine") or {}
                _put(stdscr, h - 2, 1, (_machine_line(m) + "  " + _disks_line(m))[: w - 2], curses.color_pair(4))
                _put(stdscr, h - 1, 1, _services_line(health.get("services") or {})[: w - 2])
            else:
                _put(
                    stdscr,
                    h - 1,
                    1,
                    "qwen = LiteLLM local  ·  crm = trentincrm.com.br  ·  site = trentin.software",
                    curses.A_DIM,
                )
            stdscr.refresh()
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1
            if ch in (ord("q"), ord("Q"), 27):
                break
    finally:
        watch.stop()


def _print_snapshot() -> None:
    watch = HealthWatch()
    watch.start()
    time.sleep(1.3)
    health = watch.snapshot()
    watch.stop()
    snap = _snapshot()
    print(
        f"LG v{__version__}  {snap['today_jobs']} jobs  +{snap['today_leads']} leads  "
        f"{snap['mail_sent']} e-mails",
        flush=True,
    )
    print(_machine_line(health.get("machine") or {}), flush=True)
    print(_disks_line(health.get("machine") or {}), flush=True)
    print(_services_line(health.get("services") or {}), flush=True)
    for key in ("nicho", "generalista", "mailman"):
        row = snap["workers"][key]
        mark = "●" if row["running"] else "○"
        print(f"{mark} {row['title']:<12} {row.get('progress')}  {row.get('label')}", flush=True)
        if row.get("spent") is not None:
            note = "passou da média" if row.get("overtime") else f"faltam {_fmt_dur(row.get('remain'))}"
            print(f"    neste {_fmt_clock(row['spent'])}  {note}  {row.get('detail') or ''}", flush=True)
        elif row.get("remain") is not None:
            print(f"    próximo {_fmt_dur(row['remain'])}  {row.get('last') or ''}", flush=True)
        if row.get("last_log"):
            print(f"    {row['last_log'][:100]}", flush=True)


def main() -> None:
    global _stop
    p = argparse.ArgumentParser(description="Sobe os dois hunts + mailman e abre o painel")
    p.add_argument("--attach", action="store_true", help="Só o painel")
    p.add_argument("--stop", action="store_true", help="Encerra os três e sai")
    p.add_argument("--no-tui", action="store_true", help="Sobe (se faltar) e imprime um snapshot")
    args = p.parse_args()

    if args.stop:
        print("encerrando nicho, generalista e mailman…", flush=True)
        stop_all()
        print("ok", flush=True)
        return

    if not args.attach:
        notes = {}
        for name in ("nicho", "generalista", "mailman"):
            pid, how = start_worker(name)
            notes[name] = {"pid": pid, "how": how}
            print(f"  {name:<12} {how:<10} pid {pid}", flush=True)
        session = _session_path()
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(
            json.dumps({"started_at": _now().isoformat(), "workers": notes}, indent=2),
            encoding="utf-8",
        )
        time.sleep(0.3)

    if args.no_tui or not sys.stdout.isatty():
        _print_snapshot()
        if not sys.stdout.isatty() and not args.no_tui:
            print(
                "sem TTY — processos no ar. Painel: ./cockpit --attach",
                flush=True,
            )
        return

    def _sig(_s, _f) -> None:  # noqa: ANN001
        global _stop
        _stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    curses.wrapper(_draw, time.time())
    print("painel fechado · processos seguem  ·  ./cockpit --stop", flush=True)


if __name__ == "__main__":
    main()
