#!/usr/bin/env python3
"""Revisão determinística de qualidade dos dados — sem LLM.

Dois eixos:
  1. Validade do contato (e-mail com sintaxe + domínio existente/MX,
     telefone em formato aceito pelo Espo, nome não-vazio).
  2. Paridade com o EspoCRM: os IDs gravados localmente
     (CampaignItem.crm_company_id/crm_contact_id/crm_lead_id) realmente
     existem no CRM e batem com o e-mail/nome local?

É complementar ao scripts/review_leads_llm.py (aquele julga se o lead
"faz sentido" pro nicho; este confere se os dados estão corretos e
sincronizados).

Uso:
  python scripts/review_data_quality.py                      # recentes, sem tocar em nada
  python scripts/review_data_quality.py --niche advogado -n 100
  python scripts/review_data_quality.py --campaign-id <id>
  python scripts/review_data_quality.py --skip-dns            # não confere MX (mais rápido)
  python scripts/review_data_quality.py --skip-crm            # só validação local
  python scripts/review_data_quality.py --apply-requeue       # manda de volta pro estágio
                                                                # "enriched" os itens com
                                                                # registro CRM ausente, p/
                                                                # a próxima rodada re-sincronizar
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.domain.stages import ItemStageStatus
from app.infrastructure.crm.client import CRMClient
from app.infrastructure.crm.sync import sanitize_phone
from app.infrastructure.database.models import Campaign, CampaignItem
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.providers.email_enrichment import has_valid_email, verify_email_deliverable
from app.providers.geo_email import classify_contact_email

logger = get_logger(__name__)

CRM_EXPECTED_STAGES = {ItemStageStatus.CRM_SYNCED.value, ItemStageStatus.SENT.value}


def check_local(item: CampaignItem) -> dict:
    """Validações determinísticas sem sair da memória (e-mail, telefone, nome)."""
    co = item.company
    ct = item.contact
    issues: list[str] = []

    email = (ct.email if ct else None) or (co.email if co else None) or ""
    if not email:
        issues.append("sem_email")
    elif not has_valid_email(email):
        issues.append("email_sintaxe_invalida")
    else:
        extra = (co.extra if co else None) or {}
        ok_geo, geo_reason = classify_contact_email(
            email,
            name=(ct.name if ct else "") or (co.name if co else "") or "",
            city=(co.city if co else "") or "",
            party=str(extra.get("tse_partido") or ""),
            website=(co.website if co else "") or "",
            segment=(co.segment if co else "") or "",
        )
        if not ok_geo:
            issues.append(f"email_implausivel:{geo_reason}")

    name = (ct.name if ct else "") or (co.name if co else "") or ""
    if not name.strip() or name.strip() in {".", "-"}:
        issues.append("nome_vazio_ou_lixo")

    phone = (ct.phone if ct else None) or (co.phone if co else None) or ""
    if phone and not sanitize_phone(phone):
        issues.append("telefone_formato_invalido")

    return {"email": email, "issues": issues}


async def check_crm_parity(client: CRMClient, item: CampaignItem, email: str) -> list[str]:
    """Confere se os IDs gravados localmente existem de fato no Espo e batem com o e-mail."""
    issues: list[str] = []

    if item.stage not in CRM_EXPECTED_STAGES:
        return issues

    ids = {
        "Account": item.crm_company_id,
        "Contact": item.crm_contact_id,
        "Lead": item.crm_lead_id,
    }
    if not ids["Contact"] and not ids["Lead"]:
        issues.append("crm_sem_id_gravado")
        return issues

    for entity, record_id in ids.items():
        if not record_id:
            continue
        try:
            ok = await client.exists(entity, record_id)
        except Exception as exc:
            logger.warning("crm_exists_check_failed", entity=entity, id=record_id, error=str(exc))
            issues.append(f"crm_check_falhou:{entity}")
            continue
        if not ok:
            issues.append(f"crm_{entity.lower()}_nao_existe")

    # bate e-mail só se o registro existe (evita duplo-flag)
    if ids["Contact"] and "crm_contact_nao_existe" not in issues:
        try:
            remote = await client.get("Contact", ids["Contact"])
            remote_email = (remote.get("emailAddress") or "").strip().lower()
            if email and remote_email and remote_email != email.strip().lower():
                issues.append("crm_contact_email_diverge")
        except Exception as exc:
            logger.debug("crm_contact_fetch_failed", id=ids["Contact"], error=str(exc))

    return issues


async def main() -> None:
    p = argparse.ArgumentParser(description="Revisão determinística: validade de contato + paridade CRM")
    p.add_argument("--niche", default="", help="Filtra por nicho da campanha")
    p.add_argument("--campaign-id", default="", help="Filtra por campanha")
    p.add_argument("-n", "--limit", type=int, default=150, help="Máx. itens a revisar")
    p.add_argument(
        "--stages",
        default="enriched,crm_synced,sent",
        help="Stages a incluir (csv) — só stages com contato faz sentido",
    )
    p.add_argument("--skip-dns", action="store_true", help="Não confere MX/A do domínio do e-mail")
    p.add_argument("--skip-crm", action="store_true", help="Não consulta o EspoCRM (só validação local)")
    p.add_argument(
        "--apply-requeue",
        action="store_true",
        help="Itens com registro CRM ausente voltam para stage=enriched (re-sincroniza na próxima rodada de crm)",
    )
    p.add_argument(
        "--out",
        default=str(logs_dir() / "data_quality_review.jsonl"),
        help="Arquivo JSONL de saída",
    )
    args = p.parse_args()

    setup_logging()
    reset_engine()
    await init_db()
    factory = async_session_factory()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = CRMClient() if not args.skip_crm else None
    dns_cache: dict[str, tuple[bool, str | None]] = {}

    async with factory() as session:
        q = (
            select(CampaignItem)
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
                selectinload(CampaignItem.campaign),
            )
            .where(CampaignItem.stage.in_(stages))
            .order_by(CampaignItem.created_at.desc())
            .limit(args.limit)
        )
        if args.campaign_id:
            q = q.where(CampaignItem.campaign_id == args.campaign_id)
        if args.niche:
            q = q.where(Campaign.niche == args.niche)

        items = (await session.execute(q)).scalars().all()
        print(f"Revisando {len(items)} itens (dns={'off' if args.skip_dns else 'on'} "
              f"crm={'off' if args.skip_crm else 'on'})…", flush=True)

        by_issue: Counter[str] = Counter()
        rows: list[dict] = []
        requeued = 0

        for i, item in enumerate(items, 1):
            camp = item.campaign
            local = check_local(item)
            issues = list(local["issues"])
            email = local["email"]

            if not args.skip_dns and email and "email_sintaxe_invalida" not in issues:
                domain = email.rsplit("@", 1)[-1].lower()
                if domain not in dns_cache:
                    dns_cache[domain] = await verify_email_deliverable(email)
                ok, reason = dns_cache[domain]
                if not ok:
                    issues.append("email_dominio_nao_existe")

            if client is not None:
                crm_issues = await check_crm_parity(client, item, email)
                issues.extend(crm_issues)

            for iss in issues:
                by_issue[iss] += 1

            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "item_id": item.id,
                "campaign_id": item.campaign_id,
                "niche": camp.niche if camp else "",
                "stage": item.stage,
                "email": email,
                "company": item.company.name if item.company else "",
                "issues": issues,
            }
            rows.append(row)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            flag = "✓" if not issues else "✗"
            print(
                f"[{i}/{len(items)}] {flag} niche={row['niche']:18} stage={item.stage:12} "
                f"{email or '(sem email)':35} — {', '.join(issues) or 'ok'}",
                flush=True,
            )

            if args.apply_requeue and any(
                iss.startswith("crm_") and iss != "crm_check_falhou" for iss in issues
            ):
                item.stage = ItemStageStatus.ENRICHED.value
                item.status = "email_found"
                item.crm_company_id = None
                item.crm_contact_id = None
                item.crm_lead_id = None
                item.error_message = f"data_quality_review: requeue por {issues}"[:500]
                requeued += 1

        if args.apply_requeue and requeued:
            await session.commit()
            print(f"\nRe-enfileirados {requeued} itens para nova sincronização com o CRM.", flush=True)
        else:
            await session.rollback()

    print("\n=== PARECER REVISÃO DE QUALIDADE ===")
    total = len(rows)
    ok_n = sum(1 for r in rows if not r["issues"])
    print(f"Total revisados: {total}")
    print(f"Sem problemas: {ok_n} ({100*ok_n/max(total,1):.0f}%)")
    print("\nProblemas por tipo:")
    for issue, n in by_issue.most_common():
        print(f"  {issue:32} {n:4} ({100*n/max(total,1):.0f}%)")
    print(f"\nLog: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
