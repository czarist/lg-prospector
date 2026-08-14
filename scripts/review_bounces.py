#!/usr/bin/env python3
"""Analisa bounces SMTP (NDR/DSN) na caixa IMAP e cruza com envios locais.

Caso típico: domínio/MX existe (passou no enrich_verify_email_dns), o SMTP
aceitou o envio (emails.status=sent), mas a caixa postal já foi excluída —
o provedor devolve 550/5.1.1 dias depois.

Fluxo:
  1. Conecta IMAP (IMAP_* ou reusa SMTP_* + host derivado, ex. imappro.zoho.com)
  2. Busca mensagens de mailer-daemon / undeliverable / DSN
  3. Extrai destinatário rejeitado + Message-ID original + diagnóstico
  4. Cruza com tabela `emails` (message_id ou to_address)
  5. Classifica: mailbox_missing | domain_reject | policy | unknown
  6. Opcional --apply: marca emails.status=bounced e item stage=failed

Uso (na máquina física):
  # só relatório
  python scripts/review_bounces.py --since-days 30

  # grava bounced no banco + blacklist implícita no próximo dispatch
  python scripts/review_bounces.py --since-days 30 --apply

  # confere se o domínio ainda tem MX (marca domain_alive)
  python scripts/review_bounces.py --check-domain --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.models import (
    CampaignItem,
    Contact,
    EmailRecord,
    ItemStatus,
)
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.infrastructure.email.bounce import (
    CLASS_MAILBOX_MISSING,
    BounceHit,
    normalize_message_id,
    parse_bounce_message,
)
from app.infrastructure.email.imap_client import ImapMailbox, default_imap_host
from app.providers.email_enrichment import verify_email_deliverable

logger = get_logger(__name__)

def _log_path() -> Path:
    return logs_dir() / "bounce_review.jsonl"


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _scan_imap(*, since_days: int, limit: int) -> list[BounceHit]:
    hits: list[BounceHit] = []
    with ImapMailbox() as box:
        for msg in box.iter_candidates(since_days=since_days, limit=limit):
            hit = parse_bounce_message(msg.raw, imap_uid=msg.uid)
            if hit.is_bounce or hit.recipients or hit.original_message_ids:
                hits.append(hit)
    return hits


async def _load_sent_index(session) -> tuple[dict[str, list[EmailRecord]], dict[str, list[EmailRecord]]]:
    """Índices message_id → records e to_address → records (status sent/dry_run/bounced)."""
    result = await session.execute(
        select(EmailRecord)
        .where(EmailRecord.status.in_(["sent", "dry_run", "bounced", "failed"]))
        .order_by(EmailRecord.sent_at.desc())
    )
    rows = list(result.scalars().all())
    by_mid: dict[str, list[EmailRecord]] = {}
    by_addr: dict[str, list[EmailRecord]] = {}
    for r in rows:
        mid = normalize_message_id(r.message_id)
        if mid:
            by_mid.setdefault(mid, []).append(r)
        addr = (r.to_address or "").strip().lower()
        if addr:
            by_addr.setdefault(addr, []).append(r)
    return by_mid, by_addr


def _match_records(
    hit: BounceHit,
    by_mid: dict[str, list[EmailRecord]],
    by_addr: dict[str, list[EmailRecord]],
) -> list[EmailRecord]:
    found: list[EmailRecord] = []
    seen: set[str] = set()

    for mid in hit.original_message_ids:
        for rec in by_mid.get(normalize_message_id(mid), []):
            if rec.id not in seen:
                seen.add(rec.id)
                found.append(rec)

    if found:
        return found

    # fallback: destinatário do bounce bate com envio nosso
    for addr in hit.recipients:
        for rec in by_addr.get(addr.lower(), []):
            if rec.status == "bounced":
                # ainda reporta match, mas apply será no-op
                if rec.id not in seen:
                    seen.add(rec.id)
                    found.append(rec)
                continue
            if rec.status in {"sent", "dry_run", "failed"} and rec.id not in seen:
                seen.add(rec.id)
                found.append(rec)
    return found


async def _apply_bounce(
    session,
    rec: EmailRecord,
    hit: BounceHit,
    *,
    domain_alive: bool | None,
) -> dict[str, Any]:
    """Marca EmailRecord + CampaignItem como bounced/failed."""
    diag = hit.diagnostic or hit.subject or "bounce"
    classif = hit.classification
    note_parts = [f"bounce:{classif}", diag[:240]]
    if domain_alive is True:
        note_parts.append("domain_alive=1")
    elif domain_alive is False:
        note_parts.append("domain_alive=0")
    note = " | ".join(note_parts)

    already = rec.status == "bounced"
    rec.status = "bounced"
    rec.error_message = note[:1000]

    item_updated = False
    if rec.campaign_item_id:
        item = await session.get(CampaignItem, rec.campaign_item_id)
        if item:
            item.stage = ItemStageStatus.FAILED.value
            item.status = ItemStatus.FAILED.value
            item.error_message = note[:1000]
            item_updated = True

    # anota no contact.extra
    if rec.contact_id:
        contact = await session.get(Contact, rec.contact_id)
        if contact:
            extra = dict(contact.extra or {})
            extra["email_bounce"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "classification": classif,
                "diagnostic": diag[:300],
                "domain_alive": domain_alive,
                "imap_uid": hit.imap_uid,
            }
            contact.extra = extra

    return {
        "email_id": rec.id,
        "to": rec.to_address,
        "already_bounced": already,
        "item_updated": item_updated,
        "classification": classif,
        "domain_alive": domain_alive,
        "note": note,
    }


async def run(args: argparse.Namespace) -> int:
    setup_logging()
    get_settings.cache_clear()
    settings = get_settings()

    imap_host = default_imap_host()
    print("=== REVIEW BOUNCES ===", flush=True)
    print(
        f"imap={imap_host}:{settings.imap_port} folder={settings.imap_folder} "
        f"user={settings.imap_user or settings.smtp_user} "
        f"since_days={args.since_days} limit={args.limit} "
        f"apply={args.apply} check_domain={args.check_domain}",
        flush=True,
    )

    # 1) IMAP scan (thread pool — imaplib é síncrono)
    try:
        hits = await asyncio.to_thread(
            _scan_imap, since_days=args.since_days, limit=args.limit
        )
    except Exception as exc:
        print(f"ERRO IMAP: {exc}", flush=True)
        logger.exception("bounce_imap_failed")
        return 2

    print(f"candidatos IMAP parseados: {len(hits)}", flush=True)
    if not hits:
        print("Nenhum bounce encontrado na janela.", flush=True)
        return 0

    await reset_engine()
    await init_db()

    stats: Counter[str] = Counter()
    matched_rows: list[dict[str, Any]] = []

    async with async_session_factory() as session:
        by_mid, by_addr = await _load_sent_index(session)
        print(
            f"índice local: {sum(len(v) for v in by_mid.values())} c/ message_id, "
            f"{len(by_addr)} endereços distintos",
            flush=True,
        )

        domain_cache: dict[str, bool | None] = {}

        for hit in hits:
            stats["hits_total"] += 1
            stats[f"class_{hit.classification}"] += 1

            records = _match_records(hit, by_mid, by_addr)
            if not records:
                stats["unmatched"] += 1
                row = {
                    "event": "bounce_unmatched",
                    "hit": hit.to_dict(),
                }
                _append_jsonl(_log_path(), row)
                if args.verbose:
                    print(
                        f"  ? sem match uid={hit.imap_uid} "
                        f"to={hit.recipients[:2]} mid={hit.original_message_ids[:1]} "
                        f"class={hit.classification}",
                        flush=True,
                    )
                continue

            for rec in records:
                stats["matched"] += 1
                prev_status = rec.status
                domain_alive: bool | None = None
                if args.check_domain and rec.to_address:
                    dom_key = rec.to_address.split("@")[-1].lower()
                    if dom_key not in domain_cache:
                        ok, _reason = await verify_email_deliverable(rec.to_address)
                        domain_cache[dom_key] = ok
                    domain_alive = domain_cache.get(dom_key)
                    if domain_alive and hit.classification == CLASS_MAILBOX_MISSING:
                        stats["mailbox_gone_domain_ok"] += 1

                apply_info: dict[str, Any] | None = None
                if args.apply:
                    apply_info = await _apply_bounce(
                        session, rec, hit, domain_alive=domain_alive
                    )
                    stats["applied"] += 1
                    if apply_info.get("already_bounced"):
                        stats["already_bounced"] += 1

                out = {
                    "event": "bounce_matched",
                    "applied": bool(args.apply),
                    "email_id": rec.id,
                    "to_address": rec.to_address,
                    "prev_status": prev_status,
                    "message_id": rec.message_id,
                    "campaign_item_id": rec.campaign_item_id,
                    "domain_alive": domain_alive,
                    "hit": hit.to_dict(),
                    "apply": apply_info,
                }
                matched_rows.append(out)
                _append_jsonl(_log_path(), out)
                flag = "mailbox∅+domínio✓" if domain_alive and hit.classification == CLASS_MAILBOX_MISSING else hit.classification
                print(
                    f"  {'✓' if args.apply else '·'} {rec.to_address} "
                    f"[{flag}] uid={hit.imap_uid} "
                    f"diag={(hit.diagnostic or hit.subject)[:80]}",
                    flush=True,
                )

        if args.apply:
            await session.commit()

    print("\n--- resumo ---", flush=True)
    for k, v in stats.most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"log: {_log_path()}", flush=True)

    # destaque do caso que você descreveu
    n_gone = stats.get("mailbox_gone_domain_ok", 0)
    if n_gone:
        print(
            f"\n→ {n_gone} caso(s) domínio vivo + caixa inexistente "
            f"(e-mail excluído / inventado no site).",
            flush=True,
        )
    if not args.apply and stats.get("matched"):
        print(
            "\nDry-run: nada gravado. Rode de novo com --apply para marcar bounced.",
            flush=True,
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Revisa bounces SMTP via IMAP")
    p.add_argument("--since-days", type=int, default=30, help="Janela IMAP (default 30)")
    p.add_argument("--limit", type=int, default=300, help="Máx mensagens candidatas")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Marca emails.status=bounced e campaign_items como failed",
    )
    p.add_argument(
        "--check-domain",
        action="store_true",
        help="Confere MX/domínio do destinatário (marca domain_alive)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
