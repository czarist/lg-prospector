#!/usr/bin/env python3
"""Mailman — disparo de e-mail independente da prospecção.

Avalia contatos que não receberam e-mail nos últimos 4 dias e envia
um lote de 12: 4 geral + 4 prestador + 4 nicho específico, intervalo 2–5 min.

Uso:
  python scripts/mailman.py
  python scripts/mailman.py --once
  python scripts/mailman.py --dry-run --once
  python scripts/mailman.py --plan
  python scripts/mailman.py --only generalista
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import random
import signal
import sys
from datetime import datetime, timedelta, timezone
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
from app.services.mailman_service import Lane, MailmanService

logger = get_logger(__name__)

def _mail_dir() -> Path:
    return logs_dir() / "mailman"


def _state_path() -> Path:
    return logs_dir() / "mailman_state.json"


def _pid_path() -> Path:
    return _mail_dir() / "mailman.pid"


def _lock_path() -> Path:
    return _mail_dir() / "mailman.lock"

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[mailman] sinal {signum} — finalizando após o lote atual…", flush=True)


def _acquire_lock() -> TextIO:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = lock.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SystemExit(
            "mailman já está rodando (lock em mailman.lock)"
        ) from exc
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
    mail = _mail_dir()
    mail.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    path = mail / f"sends_{day}.jsonl"
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _echo(msg: str) -> None:
    print(msg, flush=True)
    mail = _mail_dir()
    mail.mkdir(parents=True, exist_ok=True)
    with (mail / "console.log").open("a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


def _forecast_live(fc: dict[str, Any] | None) -> dict[str, Any]:
    if not fc:
        return {}
    nicho = fc.get("niche") or {}
    geral = fc.get("generalista") or {}
    return {
        "forecast": fc,
        "nicho_sent": nicho.get("sent"),
        "nicho_total": nicho.get("total"),
        "nicho_ready": nicho.get("ready"),
        "nicho_cooldown": nicho.get("cooldown"),
        "gen_sent": geral.get("sent"),
        "gen_total": geral.get("total"),
        "gen_ready": geral.get("ready"),
        "gen_waiting": geral.get("waiting"),
        "gen_cooldown": geral.get("cooldown"),
    }


def _fmt_forecast(fc: dict[str, Any] | None) -> str:
    if not fc:
        return ""
    n = fc.get("niche") or {}
    g = fc.get("generalista") or {}
    p = fc.get("prestador") or {}
    return (
        f"nicho {int(n.get('sent') or 0)}/{int(n.get('total') or 0)} "
        f"(prontos {int(n.get('ready') or 0)})  "
        f"geral {int(g.get('sent') or 0)}/{int(g.get('total') or 0)} "
        f"(prontos {int(g.get('ready') or 0)})  "
        f"prest {int(p.get('sent') or 0)}/{int(p.get('total') or 0)} "
        f"(prontos {int(p.get('ready') or 0)})"
    )


async def _forecast(
    *,
    cooldown_days: int,
    wait_days: int,
) -> dict[str, Any]:
    reset_engine()
    await init_db()
    factory = async_session_factory()
    async with factory() as session:
        data = await MailmanService(session).forecast(
            cooldown_days=cooldown_days,
            wait_days=wait_days,
        )
    await dispose_db()
    return data


def _print_batch(batch_n: int, result: dict[str, Any]) -> None:
    _echo(
        f"  lote {batch_n}: enviados={result.get('sent', 0)} "
        f"falhas={result.get('failed', 0)} pulados={result.get('skipped', 0)} "
        f"fila={result.get('candidates', 0)} "
        f"nicho={result.get('niche', '?')} gen={result.get('generalista', '?')} "
        f"prest={result.get('prestador', '?')} "
        f"escolhidos={result.get('picked', 0)} "
        f"({result.get('picked_niche', '?')}+{result.get('picked_gen', '?')}+{result.get('picked_prestador', '?')})"
    )
    for row in result.get("results") or []:
        mark = "✓" if row.get("outcome") in {"sent", "dry_run"} else "·"
        extra = ""
        if row.get("reason") and row.get("outcome") not in {"sent", "dry_run"}:
            extra = f" — {row.get('reason')}"
        _echo(
            f"    {mark} [{row.get('lane')}] {row.get('company', '')[:40]} "
            f"<{row.get('to')}> {row.get('outcome')}{extra}"
        )


async def _one_batch(
    *,
    dry_run: bool,
    batch_size: int,
    cooldown_days: int,
    wait_days: int,
    only: Lane | None,
    intra_pause: bool,
) -> dict[str, Any]:
    reset_engine()
    await init_db()
    factory = async_session_factory()
    async with factory() as session:
        svc = MailmanService(session)
        result = await svc.run_batch(
            dry_run=dry_run,
            batch_size=batch_size,
            cooldown_days=cooldown_days,
            wait_days=wait_days,
            only=only,
            intra_pause=intra_pause,
        )
        await session.commit()
    await dispose_db()
    return result


async def _plan(
    *,
    cooldown_days: int,
    wait_days: int,
    only: Lane | None,
) -> dict[str, Any]:
    reset_engine()
    await init_db()
    factory = async_session_factory()
    async with factory() as session:
        svc = MailmanService(session)
        preview = await svc.preview(
            cooldown_days=cooldown_days,
            wait_days=wait_days,
            only=only,
        )
    await dispose_db()
    return preview


def _random_interval(lo: float, hi: float) -> float:
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


async def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Mailman — disparo lento de e-mail (fora do hunt)")
    p.add_argument("--once", action="store_true", help="Um lote e encerra")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Monta e-mails sem SMTP real (ainda grava EmailRecord / cooldown)",
    )
    p.add_argument("--plan", action="store_true", help="Só lista a fila e sai")
    p.add_argument(
        "--only",
        choices=("niche", "generalista", "prestador"),
        default="",
        help="Restringe a uma faixa (default: 4+4+4)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=settings.mailman_batch_size,
        help="Disparos por lote (default 12 = 4 geral + 4 prestador + 4 nicho)",
    )
    p.add_argument(
        "--min-interval",
        type=float,
        default=settings.mailman_interval_min_seconds,
        help="Pausa mínima entre lotes, em segundos (default 120)",
    )
    p.add_argument(
        "--max-interval",
        type=float,
        default=settings.mailman_interval_max_seconds,
        help="Pausa máxima entre lotes, em segundos (default 300)",
    )
    p.add_argument(
        "--cooldown-days",
        type=int,
        default=settings.email_cooldown_days,
        help="Não reenviar se já houve e-mail nos últimos N dias",
    )
    p.add_argument(
        "--wait-days",
        type=int,
        default=settings.email_cooldown_days,
        help="Espera após cadastro de lead generalista novo (default 4)",
    )
    p.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Para após N lotes (0=∞)",
    )
    p.add_argument(
        "--no-intra-pause",
        action="store_true",
        help="Sem pausa entre os e-mails do mesmo lote",
    )
    p.add_argument(
        "--empty-wait",
        type=float,
        default=90.0,
        help="Espera quando a fila está vazia (segundos)",
    )
    args = p.parse_args()

    setup_logging()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    only: Lane | None = args.only or None  # type: ignore[assignment]
    if args.only == "":
        only = None

    if args.plan:
        preview = await _plan(
            cooldown_days=args.cooldown_days,
            wait_days=args.wait_days,
            only=only,
        )
        print(
            f"Fila mailman: {preview['candidates']} "
            f"(nicho={preview['niche']} generalista={preview['generalista']}) "
            f"cooldown={preview['cooldown_days']}d wait={preview['wait_days']}d",
            flush=True,
        )
        fc = preview.get("forecast") or {}
        if fc:
            print(f"Previsão: {_fmt_forecast(fc)}", flush=True)
        for row in preview.get("sample") or []:
            print(
                f"  [{row['lane']:12}] {row.get('niche', ''):18} "
                f"{(row.get('company') or '')[:40]:40} <{row.get('to')}> "
                f"{row.get('city') or ''}",
                flush=True,
            )
        return

    lock_fh = _acquire_lock()
    try:
        _echo("=== MAILMAN ===")
        _echo(
            f"lote={args.batch_size} intervalo={args.min_interval:.0f}–{args.max_interval:.0f}s "
            f"cooldown={args.cooldown_days}d wait_generalista={args.wait_days}d "
            f"only={only or 'nicho+generalista'} dry_run={args.dry_run}"
        )

        state = _load_state()
        batches = int(state.get("batches") or 0)
        sent_total = int(state.get("sent_total") or 0)
        failed_total = int(state.get("failed_total") or 0)

        try:
            boot_fc = await _forecast(
                cooldown_days=args.cooldown_days,
                wait_days=args.wait_days,
            )
            _echo(f"previsão  {_fmt_forecast(boot_fc)}")
            write_live(
                "mailman",
                {
                    "status": "running",
                    "phase": "fila",
                    "sent_total": sent_total,
                    "failed_total": failed_total,
                    **_forecast_live(boot_fc),
                },
            )
        except Exception as exc:
            logger.exception("mailman_forecast_error")
            _echo(f"  aviso previsão: {exc}")

        while not _stop:
            batches += 1
            try:
                result = await _one_batch(
                    dry_run=args.dry_run,
                    batch_size=args.batch_size,
                    cooldown_days=args.cooldown_days,
                    wait_days=args.wait_days,
                    only=only,
                    intra_pause=not args.no_intra_pause,
                )
            except Exception as exc:
                logger.exception("mailman_batch_error")
                _echo(f"  ERRO lote {batches}: {exc}")
                _log_row({"event": "error", "batch": batches, "error": str(exc)})
                result = {"error": str(exc), "sent": 0, "failed": 0, "provider_blocked": False}

            _print_batch(batches, result)
            _log_row({"event": "batch", "batch": batches, "result": result})
            sent_n = int(result.get("sent") or 0)
            last_line = ""
            rows = result.get("results") or []
            if rows:
                bits = [f"{r.get('lane')}" for r in rows if r.get("outcome") in {"sent", "dry_run"}]
                last_line = " + ".join(bits) if bits else (rows[0].get("outcome") or "")
            write_live(
                "mailman",
                {
                    "status": "running",
                    "phase": "lote",
                    "batch": batches,
                    "sent_total": sent_total + sent_n,
                    "failed_total": failed_total + int(result.get("failed") or 0),
                    "candidates": result.get("candidates"),
                    "niche_n": result.get("niche"),
                    "gen_n": result.get("generalista"),
                    "picked": result.get("picked"),
                    "last_line": last_line,
                    **_forecast_live(result.get("forecast")),
                },
            )
            sent_total += int(result.get("sent") or 0)
            failed_total += int(result.get("failed") or 0)
            state_keys = (
                "sent",
                "failed",
                "skipped",
                "candidates",
                "niche",
                "generalista",
                "picked",
                "provider_blocked",
            )
            state = {
                "batches": batches,
                "sent_total": sent_total,
                "failed_total": failed_total,
                "last_result": {k: result.get(k) for k in state_keys},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_state(state)

            if result.get("provider_blocked"):
                _echo("  ✗ SMTP bloqueado (Zoho 550). Encerrando mailman.")
                break

            if args.once or _stop:
                break
            if args.max_batches and batches >= args.max_batches:
                _echo("max-batches atingido")
                break

            empty = int(result.get("picked") or 0) == 0 or (
                int(result.get("sent") or 0) == 0
                and int(result.get("failed") or 0) == 0
                and not result.get("provider_blocked")
            )
            if empty:
                wait = max(15.0, float(args.empty_wait))
                _echo(
                    f"  fila vazia — nicho={result.get('niche', 0)} "
                    f"generalista={result.get('generalista', 0)} "
                    f"— aguardando {wait:.0f}s…"
                )
            else:
                wait = _random_interval(args.min_interval, args.max_interval)
                _echo(f"  pausa {wait:.0f}s até o próximo lote…")
            write_live(
                "mailman",
                {
                    "phase": "pausa" if not empty else "fila_vazia",
                    "next_at": (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat(),
                    "wait_s": round(wait),
                },
            )
            slept = 0.0
            last_fc = 0.0
            while slept < wait and not _stop:
                step = min(5.0, wait - slept)
                await asyncio.sleep(step)
                slept += step
                if slept - last_fc >= 30 and not _stop:
                    last_fc = slept
                    try:
                        fc = await _forecast(
                            cooldown_days=args.cooldown_days,
                            wait_days=args.wait_days,
                        )
                        write_live("mailman", _forecast_live(fc))
                    except Exception:
                        logger.exception("mailman_forecast_refresh")

        _echo(
            f"\n=== FIM mailman lotes={batches} enviados={sent_total} falhas={failed_total} ==="
        )
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass
        try:
            pid_path = _pid_path()
            if pid_path.is_file():
                pid_path.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
