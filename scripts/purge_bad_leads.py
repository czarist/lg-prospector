#!/usr/bin/env python3
"""Remove leads que não encaixam no nicho ou na nacionalidade (BR).

Usa o critério do pipeline (judge_lead). Antes de excluir e-mail
gratuito (Gmail, Hotmail, Outlook, Yahoo, UOL…) ou .com/.net/.org
o Qwen local avalia se o endereço é do negócio — evita apagar
fulanaadvogada@gmail.com / contato@hotmail.com.

Dry-run por padrão. Com --apply:
  1. Apaga Lead / Contact / Account no EspoCRM
  2. Marca campaign_items como discarded
  3. Apaga contacts e companies no banco local

Uso:
  # relatório (não grava)
  python scripts/purge_bad_leads.py
  python scripts/purge_bad_leads.py --niche politico -n 500
  python scripts/purge_bad_leads.py --since-days 7 --city "São Paulo"

  # só banco local (não toca o Espo)
  python scripts/purge_bad_leads.py --apply --skip-crm

  # CRM + banco
  python scripts/purge_bad_leads.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.domain.stages import ItemStageStatus
from app.infrastructure.crm.client import CRMClient
from app.infrastructure.database.models import (
    CampaignItem,
    Company,
    Contact,
    ItemStatus,
)
from app.infrastructure.database.session import (
    async_session_factory,
    dispose_db,
    init_db,
    reset_engine,
)
from app.providers.geo_email import email_needs_llm_review, judge_lead

logger = get_logger(__name__)

def _log_path() -> Path:
    return logs_dir() / "purge_bad_leads.jsonl"


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _party_of(company: Company) -> str:
    extra = company.extra or {}
    return str(extra.get("tse_partido") or extra.get("partido") or "")


def _snippet_of(company: Company) -> str:
    extra = company.extra or {}
    return str(extra.get("snippet") or "")


_HARD_REASONS = (
    "fora_do_nicho_ou_nacionalidade",
    "email:orgao_publico",
    "email:tld_estrangeiro",
    "email:dominio_lixo",
    "email:sintaxe",
    "email:site_diretorio",
    "email:nome_lixo",
    "email:nao_e_negocio",
)


def _is_hard_reason(reason: str) -> bool:
    return any(reason == h or reason.startswith(h) for h in _HARD_REASONS)


def judge_company(company: Company, contacts: list[Contact]) -> tuple[bool, list[str]]:
    """keep, reasons — reprova se a empresa ou QUALQUER e-mail principal falhar."""
    emails: list[tuple[str, str]] = []
    if company.email:
        emails.append((company.email, company.name or ""))
    for ct in contacts:
        if ct.email:
            emails.append((ct.email, ct.name or company.name or ""))

    if emails:
        reasons: list[str] = []
        any_ok = False
        for em, cname in emails:
            keep, why = judge_lead(
                name=company.name or "",
                email=em,
                website=company.website or "",
                city=company.city or "",
                segment=company.segment or "",
                snippet=_snippet_of(company),
                party=_party_of(company),
                contact_name=cname,
            )
            if keep:
                any_ok = True
            else:
                reasons.extend(why)
        # se nenhum e-mail passa, ou a empresa em si é lixo
        if not any_ok:
            return False, list(dict.fromkeys(reasons)) or ["email_invalido"]
        # e-mail bom mas empresa/site é diretório / estrangeiro / fora do nicho
        lead_ok, lead_why = judge_lead(
            name=company.name or "",
            email="",
            website=company.website or "",
            city=company.city or "",
            segment=company.segment or "",
            snippet=_snippet_of(company),
            party=_party_of(company),
        )
        if not lead_ok:
            return False, lead_why
        return True, []

    keep, reasons = judge_lead(
        name=company.name or "",
        email="",
        website=company.website or "",
        city=company.city or "",
        segment=company.segment or "",
        snippet=_snippet_of(company),
        party=_party_of(company),
    )
    return keep, reasons


async def judge_company_with_llm(
    company: Company,
    contacts: list[Contact],
    *,
    use_llm: bool,
) -> tuple[bool, list[str], dict[str, Any] | None]:
    """Se o único problema é e-mail genérico, o Qwen decide se mantém."""
    keep, reasons = judge_company(company, contacts)
    if keep or not use_llm:
        return keep, reasons, None
    if any(_is_hard_reason(r) for r in reasons):
        return False, reasons, None

    emails: list[tuple[str, str]] = []
    if company.email:
        emails.append((company.email, company.name or ""))
    for ct in contacts:
        if ct.email:
            emails.append((ct.email, ct.name or company.name or ""))
    reviewable = [(e, n) for e, n in emails if email_needs_llm_review(e)]
    if not reviewable:
        return False, reasons, None

    from app.infrastructure.llm.client import score_email_belongs_to_business

    verdicts: list[dict[str, Any]] = []
    any_keep = False
    for em, cname in reviewable:
        verdict = await score_email_belongs_to_business(
            email=em,
            name=cname or company.name or "",
            website=company.website or "",
            city=company.city or "",
            segment=company.segment or "",
            snippet=_snippet_of(company),
            force=True,
        )
        verdicts.append({"email": em, **verdict})
        if verdict.get("keep", False):
            any_keep = True
            logger.info(
                "purge_llm_keep",
                email=em,
                company=company.name,
                reason=verdict.get("reason"),
                score=verdict.get("score"),
            )

    extra = {"llm_email": verdicts}
    if any_keep:
        return True, [], extra
    return False, reasons + ["llm_email:drop"], extra


def _crm_ids_for(
    company: Company,
    contacts: list[Contact],
    items: list[CampaignItem],
) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = {"Account": set(), "Contact": set(), "Lead": set()}
    if company.crm_id:
        ids["Account"].add(company.crm_id)
    for ct in contacts:
        if ct.crm_id:
            ids["Contact"].add(ct.crm_id)
    for it in items:
        if it.crm_company_id:
            ids["Account"].add(it.crm_company_id)
        if it.crm_contact_id:
            ids["Contact"].add(it.crm_contact_id)
        if it.crm_lead_id:
            ids["Lead"].add(it.crm_lead_id)
    return ids


async def _delete_crm(
    client: CRMClient,
    ids: dict[str, set[str]],
    *,
    skip_accounts: set[str],
    stats: Counter,
) -> dict[str, list[str]]:
    """Lead → Contact → Account. Contas compartilhadas ficam de fora."""
    done: dict[str, list[str]] = {"Lead": [], "Contact": [], "Account": []}
    for entity in ("Lead", "Contact", "Account"):
        for rid in ids.get(entity, ()):
            if entity == "Account" and rid in skip_accounts:
                stats["crm_account_shared_kept"] += 1
                continue
            result = await client.delete_if_exists(entity, rid)
            stats[f"crm_{entity.lower()}_{result}"] += 1
            if result == "deleted":
                done[entity].append(rid)
            await asyncio.sleep(0.05)
    return done


async def main() -> None:
    p = argparse.ArgumentParser(
        description="Remove do CRM e do banco local leads fora do nicho/nacionalidade"
    )
    p.add_argument("--niche", default="", help="Filtra por segment/nicho")
    p.add_argument("--city", default="", help="Filtra por cidade")
    p.add_argument("--since-days", type=int, default=0, help="Só empresas criadas nos últimos N dias (0=todas)")
    p.add_argument("-n", "--limit", type=int, default=0, help="Máx. empresas a varrer (0=todas)")
    p.add_argument(
        "--only-with-email",
        action="store_true",
        help="Ignora empresas ainda sem e-mail (discovered)",
    )
    p.add_argument("--skip-crm", action="store_true", help="Não apaga no EspoCRM")
    p.add_argument(
        "--apply",
        action="store_true",
        help="GRAVA: apaga CRM + contacts/companies locais e marca itens discarded",
    )
    p.add_argument(
        "--skip-llm",
        action="store_true",
        help="Não consulta o Qwen (mais rápido; pode marcar Gmail/Hotmail/.com válidos para purge)",
    )
    p.add_argument("--quiet", action="store_true", help="Só o parecer final (sem linha a linha)")
    p.add_argument("--out", default=str(_log_path()), help="JSONL de saída")
    args = p.parse_args()

    setup_logging()
    reset_engine()
    await init_db()
    factory = async_session_factory()
    out_path = Path(args.out)

    client: CRMClient | None = None
    if args.apply and not args.skip_crm:
        client = CRMClient()
        if not client._enabled:
            print("CRM desabilitado (sem URL/credencial) — só banco local.", flush=True)
            client = None
        else:
            try:
                await client.authenticate()
            except Exception as exc:
                print(f"Falha ao autenticar no CRM: {exc}", flush=True)
                print("Abortando. Use --skip-crm para limpar só o banco.", flush=True)
                raise SystemExit(2)

    async with factory() as session:
        q = (
            select(Company)
            .options(selectinload(Company.contacts))
            .order_by(Company.created_at.desc())
        )
        if args.niche:
            q = q.where(Company.segment == args.niche)
        if args.city:
            q = q.where(Company.city == args.city)
        if args.since_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.since_days)
            q = q.where(Company.created_at >= cutoff)
        if args.limit > 0:
            q = q.limit(args.limit)

        companies = (await session.execute(q)).scalars().unique().all()
        if args.only_with_email:
            companies = [
                c
                for c in companies
                if (c.email or any(ct.email for ct in (c.contacts or [])))
            ]

        company_ids = [c.id for c in companies]
        items_by_co: dict[str, list[CampaignItem]] = defaultdict(list)
        if company_ids:
            # IN enorme: fatia de 400
            for i in range(0, len(company_ids), 400):
                chunk = company_ids[i : i + 400]
                rows = (
                    await session.execute(
                        select(CampaignItem).where(CampaignItem.company_id.in_(chunk))
                    )
                ).scalars().all()
                for it in rows:
                    if it.company_id:
                        items_by_co[it.company_id].append(it)

        # contas CRM usadas por empresas que VÃO ficar
        keep: list[Company] = []
        purge: list[tuple[Company, list[str]]] = []
        llm_kept = 0
        use_llm = not args.skip_llm
        print(
            f"Avaliando {len(companies)} empresas"
            f"{' (Qwen em Gmail/Hotmail/Outlook/.com/.net/.org)' if use_llm else ' (sem LLM)'}…",
            flush=True,
        )
        for i, co in enumerate(companies, 1):
            ok, reasons, llm_meta = await judge_company_with_llm(
                co, list(co.contacts or []), use_llm=use_llm
            )
            if ok:
                keep.append(co)
                if llm_meta and llm_meta.get("llm_email"):
                    llm_kept += 1
                    extra_llm = dict(co.extra or {})
                    extra_llm["purge_llm_email"] = llm_meta["llm_email"]
                    co.extra = extra_llm
            else:
                purge.append((co, reasons))
            if use_llm and not args.quiet and i % 25 == 0:
                print(f"  … {i}/{len(companies)}  manter={len(keep)} remover={len(purge)}", flush=True)

        keep_account_ids: set[str] = set()
        purge_ids = {co.id for co, _ in purge}
        for co in keep:
            if co.crm_id:
                keep_account_ids.add(co.crm_id)
            for it in items_by_co.get(co.id, []):
                if it.crm_company_id:
                    keep_account_ids.add(it.crm_company_id)

        stats: Counter[str] = Counter()
        stats["scanned"] = len(companies)
        stats["keep"] = len(keep)
        stats["purge"] = len(purge)
        stats["llm_salvou"] = llm_kept

        print(
            f"Varredura: {len(companies)} empresas  "
            f"manter={len(keep)}  remover={len(purge)}  "
            f"apply={args.apply} skip_crm={args.skip_crm or client is None}",
            flush=True,
        )

        by_reason: Counter[str] = Counter()
        by_niche: Counter[str] = Counter()

        for i, (co, reasons) in enumerate(purge, 1):
            contacts = list(co.contacts or [])
            items = items_by_co.get(co.id, [])
            email = (contacts[0].email if contacts and contacts[0].email else None) or co.email or ""
            for r in reasons:
                by_reason[r] += 1
            by_niche[co.segment or "?"] += 1

            row = {
                "action": "purge" if args.apply else "would_purge",
                "company_id": co.id,
                "name": co.name,
                "email": email,
                "website": co.website or "",
                "city": co.city or "",
                "state": co.state or "",
                "segment": co.segment or "",
                "reasons": reasons,
                "contacts": len(contacts),
                "items": len(items),
                "crm": {
                    "account": co.crm_id,
                    "contacts": [ct.crm_id for ct in contacts if ct.crm_id],
                    "items": [
                        {
                            "id": it.id,
                            "account": it.crm_company_id,
                            "contact": it.crm_contact_id,
                            "lead": it.crm_lead_id,
                        }
                        for it in items
                    ],
                },
            }

            if not args.quiet:
                flag = "APAGA" if args.apply else "lixo"
                print(
                    f"[{i}/{len(purge)}] {flag} {co.segment or '?':18} "
                    f"{(co.city or ''):18} {(email or '(sem email)'):36} "
                    f"{(co.name or '')[:40]} — {', '.join(reasons)}",
                    flush=True,
                )

            if args.apply:
                crm_ids = _crm_ids_for(co, contacts, items)
                if client is not None:
                    await _delete_crm(
                        client, crm_ids, skip_accounts=keep_account_ids, stats=stats
                    )
                else:
                    stats["crm_skipped"] += 1

                for it in items:
                    it.stage = ItemStageStatus.DISCARDED.value
                    it.status = ItemStatus.SKIPPED.value
                    it.error_message = f"purge_bad_leads:{','.join(reasons)}"[:500]
                    it.crm_company_id = None
                    it.crm_contact_id = None
                    it.crm_lead_id = None
                    it.contact_id = None
                    it.company_id = None
                    stats["items_discarded"] += 1

                for ct in contacts:
                    await session.delete(ct)
                    stats["contacts_deleted"] += 1
                await session.delete(co)
                stats["companies_deleted"] += 1
                row["applied"] = True

            _append_jsonl(out_path, row)

            if args.apply and i % 25 == 0:
                await session.commit()
                print(f"  … commit parcial ({i}/{len(purge)})", flush=True)

        if args.apply:
            await session.commit()
        else:
            await session.rollback()

    print("\n=== PARECER PURGE ===")
    print(f"Varridas:     {stats['scanned']}")
    print(f"Mantidas:     {stats['keep']}")
    print(f"  (Qwen salvou): {stats.get('llm_salvou', 0)}")
    print(f"Remover:      {stats['purge']}")
    if by_niche:
        print("\nPor nicho:")
        for k, n in by_niche.most_common():
            print(f"  {k:20} {n:4}")
    if by_reason:
        print("\nMotivos:")
        for k, n in by_reason.most_common():
            print(f"  {k:40} {n:4}")
    if args.apply:
        print("\nAplicado:")
        for k, n in stats.most_common():
            if k in {"scanned", "keep", "purge"}:
                continue
            print(f"  {k:32} {n}")
    else:
        print("\nDry-run — nada gravado. Rode com --apply para apagar CRM + banco.")
    print(f"\nLog: {out_path}")
    await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
