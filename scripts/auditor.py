#!/usr/bin/env python3
"""Auditor — revalida leads já cadastrados com modelos remotos (LiteLLM).

Não usa Qwen local. Lê bounces da caixa IMAP, busca e-mail melhor se a
caixa não existe, e tenta salvar o contato. Cada lead é revisado ao menos
uma vez.

Uso:
  python scripts/auditor.py --once --dry-run   # testa 1, não grava
  python scripts/auditor.py --once             # 1 lead, grava se recuperar e-mail
  python scripts/auditor.py                    # fila contínua
  python scripts/auditor.py --reset            # zera 'já revisado' e sai
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
from app.core.live import live_path, write_live
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.infrastructure.database.session import (
    async_session_factory,
    dispose_db,
    init_db,
    reset_engine,
)
from app.infrastructure.llm.client import is_local_llm_model
from app.services.auditor_service import AuditorService

logger = get_logger(__name__)


def _dir() -> Path:
    return logs_dir() / "auditor"


def _state_path() -> Path:
    return logs_dir() / "auditor_state.json"


def _pid_path() -> Path:
    return _dir() / "auditor.pid"


def _lock_path() -> Path:
    return _dir() / "auditor.lock"


_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[auditor] sinal {signum} — finalizando após o item atual…", flush=True)


def _acquire_lock() -> TextIO:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = lock.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SystemExit("auditor já está rodando (lock em auditor.lock)") from exc
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
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    path = folder / f"audits_{day}.jsonl"
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _echo(msg: str) -> None:
    print(msg, flush=True)
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "console.log").open("a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


def _live(**kwargs: Any) -> None:
    write_live("auditor", kwargs)


async def _reset() -> int:
    setup_logging()
    get_settings.cache_clear()
    reset_engine()
    await init_db()
    factory = async_session_factory()
    try:
        async with factory() as session:
            svc = AuditorService(session)
            stats = await svc.reset_reviews()
    finally:
        await dispose_db()

    _save_state(
        {
            "reviewed": 0,
            "recovered": 0,
            "kept": 0,
            "enriched": 0,
            "reset_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    live = live_path("auditor")
    if live.is_file():
        live.unlink(missing_ok=True)
    _live(
        phase="parado",
        pending=stats["pending"],
        reviewed=0,
        recovered=0,
        kept=0,
        enriched=0,
        last_line="reset: fila do zero",
        last_action="reset",
    )
    _echo(
        f"reset ok  apagou marca em {stats['cleared']} contatos  "
        f"fila={stats['pending']}"
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    global _stop
    setup_logging()
    settings = get_settings()
    if getattr(args, "reset", False):
        return await _reset()
    if is_local_llm_model(settings.auditor_model):
        _echo("ERRO: AUDITOR_MODEL não pode ser Qwen/local-main")
        return 2
    pause = max(0.0, float(args.pause if args.pause is not None else settings.auditor_pause_seconds))

    state = _load_state()
    reviewed = int(state.get("reviewed") or 0)
    recovered = int(state.get("recovered") or 0)
    kept = int(state.get("kept") or 0)
    enriched = int(state.get("enriched") or 0)

    _echo(
        f"=== AUDITOR  model={settings.auditor_model} "
        f"fallbacks={settings.auditor_fallback_models} "
        f"tokens={settings.auditor_max_tokens} "
        f"dry_run={args.dry_run} ==="
    )
    _live(
        phase="aguardando",
        reviewed=reviewed,
        recovered=recovered,
        kept=kept,
        enriched=enriched,
        model=settings.auditor_model,
        last_line="fila",
    )

    get_settings.cache_clear()
    reset_engine()
    await init_db()
    factory = async_session_factory()
    imap_ok = True
    skipped: set[str] = set()

    try:
        async with factory() as session:
            svc = AuditorService(session)
            try:
                n_b = await svc.load_bounce_index(imap=not args.skip_imap)
                _echo(f"bounces indexados: {n_b}")
            except Exception as exc:
                imap_ok = False
                _echo(f"  aviso IMAP/DB bounce: {exc}")
                logger.warning("auditor_bounce_index_failed", error=str(exc))

            while not _stop:
                ct = None
                ct_id = ""
                name = ""
                pending = 0
                try:
                    pending = await svc.count_pending()
                    contacts = await svc.next_contacts(limit=1, exclude_ids=skipped)
                    if not contacts:
                        _live(
                            phase="aguardando",
                            pending=pending,
                            last_line="sem leads pendentes",
                        )
                        if args.once:
                            _echo("nenhum contato pendente de auditoria")
                            break
                        _echo(f"  (0 pendentes na query; count={pending}) — espera")
                        await asyncio.sleep(max(8.0, pause))
                        continue

                    ct = contacts[0]
                    ct_id = ct.id
                    name = (ct.company.name if ct.company else ct.name) or ""
                    _echo(f"  ? [{pending} na fila] {name}  <{ct.email}>")
                    _live(
                        phase="validando",
                        pending=pending,
                        last_name=name[:50],
                        last_email=ct.email,
                    )

                    result = await svc.audit_contact(ct, dry_run=args.dry_run)
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
                    logger.exception(
                        "auditor_item_failed",
                        contact_id=ct_id or None,
                        name=name[:80],
                    )
                    _echo(f"  ERRO {name[:40]}  {err[:160]}")
                    if ct_id:
                        skipped.add(ct_id)
                    try:
                        await session.rollback()
                    except Exception:
                        logger.exception("auditor_rollback_failed")
                    _log_row(
                        {
                            "event": "error",
                            "contact_id": ct_id or None,
                            "company": name,
                            "error": err[:400],
                            "imap_ok": imap_ok,
                        }
                    )
                    _live(
                        phase="aguardando",
                        pending=pending,
                        last_line=f"erro {name[:36]} {err}"[:140],
                        last_name=name[:50],
                        last_action="erro",
                    )
                    if args.once:
                        break
                    await asyncio.sleep(max(2.0, pause))
                    continue

                reviewed += 1
                gaps = [str(g) for g in (result.get("gaps_filled") or []) if g]
                if result.get("new_email"):
                    recovered += 1
                else:
                    kept += 1
                if gaps:
                    enriched += 1
                line = (
                    f"{result.get('action')} {result.get('company', '')[:36]}  "
                    f"{result.get('email')}  bounce={result.get('bounce') or 'não'}  "
                    f"{result.get('reason') or ''}"
                )[:140]
                if result.get("new_email"):
                    line = (
                        f"RECUPEROU {result['email']} → {result['new_email']}  "
                        f"{result.get('reason') or ''}"
                    )[:140]
                _echo(f"  {line}")
                if gaps:
                    _echo(f"    lacunas: {', '.join(gaps)}")
                analysis = (result.get("analysis") or "").strip()
                if analysis:
                    _echo(f"    {analysis[:220]}")
                _log_row({"event": "audit", **result, "imap_ok": imap_ok})
                _save_state(
                    {
                        "reviewed": reviewed,
                        "recovered": recovered,
                        "kept": kept,
                        "enriched": enriched,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _live(
                    phase="aguardando",
                    pending=max(0, pending - 1),
                    reviewed=reviewed,
                    recovered=recovered,
                    kept=kept,
                    enriched=enriched,
                    last_line=line,
                    last_name=name[:50],
                    last_action=result.get("action"),
                    last_gaps=",".join(gaps),
                    model=result.get("model"),
                )
                if args.once:
                    break
                if pause:
                    await asyncio.sleep(pause)
    finally:
        await dispose_db()

    _echo(
        f"=== FIM auditor reviewed={reviewed} recovered={recovered} "
        f"kept={kept} enriched={enriched} ==="
    )
    _live(
        phase="parado",
        reviewed=reviewed,
        recovered=recovered,
        kept=kept,
        enriched=enriched,
    )
    return 0


def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Revalida leads cadastrados (modelos remotos)")
    p.add_argument("--once", action="store_true", help="Um contato e sai")
    p.add_argument("--dry-run", action="store_true", help="Não grava e-mail novo")
    p.add_argument(
        "--reset",
        action="store_true",
        help="Apaga a marca de revisado e zera o contador — próxima subida começa do zero",
    )
    p.add_argument("--skip-imap", action="store_true", help="Não lê a caixa (só bounce no banco)")
    p.add_argument(
        "--pause",
        type=float,
        default=None,
        help=f"Pausa entre itens (default {settings.auditor_pause_seconds})",
    )
    args = p.parse_args()

    _dir().mkdir(parents=True, exist_ok=True)
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
