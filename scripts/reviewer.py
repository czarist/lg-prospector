#!/usr/bin/env python3
"""Reviewer — valida candidatos na IA local, separado da caçada.

A busca (hunt_loop / hunt_generalista) só acha o lead e enfileira.
Este processo consome a fila Redis, pergunta ao Qwen e grava ou descarta.

Uso:
  python scripts/reviewer.py
  python scripts/reviewer.py --once
  python scripts/reviewer.py --dry-run --once
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.live import write_live
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.infrastructure.database.session import (
    async_session_factory,
    dispose_db,
    init_db,
    reset_engine,
)
from app.services.review_queue import ReviewQueue
from app.services.review_service import ReviewService, review_live_line

logger = get_logger(__name__)


def _rev_dir() -> Path:
    return logs_dir() / "reviewer"


def _state_path() -> Path:
    return logs_dir() / "reviewer_state.json"


def _pid_path() -> Path:
    return _rev_dir() / "reviewer.pid"


def _lock_path() -> Path:
    return _rev_dir() / "reviewer.lock"


_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[reviewer] sinal {signum} — finalizando após o item atual…", flush=True)


def _acquire_lock() -> TextIO:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = lock.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SystemExit("reviewer já está rodando (lock em reviewer.lock)") from exc
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _pid_path().write_text(str(os.getpid()), encoding="utf-8")
    return fh


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_row(row: dict[str, Any]) -> None:
    folder = _rev_dir()
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    path = folder / f"reviews_{day}.jsonl"
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _echo(msg: str) -> None:
    print(msg, flush=True)
    folder = _rev_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "console.log").open("a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


def _live(**kwargs: Any) -> None:
    write_live("reviewer", kwargs)


async def _run(args: argparse.Namespace) -> int:
    global _stop
    setup_logging()
    settings = get_settings()
    queue = ReviewQueue()
    pause = max(0.0, float(args.pause if args.pause is not None else settings.review_pause_seconds))

    state = _load_state()
    kept_total = int(state.get("kept_total") or 0)
    dropped_total = int(state.get("dropped_total") or 0)
    processed = 0
    factory = None

    _echo(
        f"=== REVIEWER  llm={settings.hunt_use_llm} model={settings.model} "
        f"fila={queue.name} pausa={pause}s ==="
    )
    _live(
        phase="aguardando",
        queue=await queue.length(),
        kept_total=kept_total,
        dropped_total=dropped_total,
        last_line="fila",
    )

    while not _stop:
        pending = await queue.length()
        _live(phase="aguardando" if pending == 0 else "validando", queue=pending)
        payload = await queue.dequeue(timeout=2)
        if payload is None:
            if args.once:
                break
            continue

        name = str((payload.get("candidate") or {}).get("company_name") or "")[:50]
        niche = payload.get("niche") or ""
        _echo(f"  ? {niche}  {name}")
        _live(phase="validando", last_name=name, niche=niche, queue=pending)

        if args.dry_run:
            await queue.requeue(payload)
            result = {"outcome": "dry_run", "name": name, "reason": "dry_run"}
        else:
            if factory is None:
                get_settings.cache_clear()
                reset_engine()
                await init_db()
                factory = async_session_factory()
            try:
                async with factory() as session:
                    svc = ReviewService(session)
                    result = await svc.process_payload(payload)
            except Exception as exc:
                logger.exception("reviewer_item_error")
                result = {"outcome": "retry", "name": name, "reason": str(exc)[:120]}
                payload["attempts"] = int(payload.get("attempts") or 0) + 1
                if payload["attempts"] < 3:
                    await queue.requeue(payload)
                await dispose_db()
                factory = None

        processed += 1
        outcome = result.get("outcome") or ""
        if outcome in {"keep", "keep_geral"}:
            kept_total += 1
        elif outcome == "drop":
            dropped_total += 1
        line = review_live_line(result)
        _echo(f"  {line}")
        _log_row({"event": "review", **result, "niche": niche})
        state = {
            "kept_total": kept_total,
            "dropped_total": dropped_total,
            "processed": processed,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_state(state)
        _live(
            phase="aguardando",
            queue=await queue.length(),
            kept_total=kept_total,
            dropped_total=dropped_total,
            last_line=line,
            last_name=result.get("name") or name,
            last_outcome=outcome,
            last_score=result.get("score"),
        )
        if args.once:
            break
        if pause and outcome != "retry":
            await asyncio.sleep(pause)

    if factory is not None:
        await dispose_db()
    _echo(f"=== FIM reviewer processados={processed} keep={kept_total} drop={dropped_total} ===")
    _live(phase="parado", queue=await queue.length(), kept_total=kept_total, dropped_total=dropped_total)
    return 0


def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Valida leads na IA local (fila Redis)")
    p.add_argument("--once", action="store_true", help="Processa um item (ou espera 2s) e sai")
    p.add_argument("--dry-run", action="store_true", help="Não grava; só tira da fila")
    p.add_argument(
        "--pause",
        type=float,
        default=None,
        help=f"Pausa entre itens (default {settings.review_pause_seconds})",
    )
    args = p.parse_args()

    _rev_dir().mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        raise SystemExit(asyncio.run(_run(args)))
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()
        try:
            _pid_path().unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
