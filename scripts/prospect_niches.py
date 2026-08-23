#!/usr/bin/env python3
"""Prospecta N clientes por nicho COM E-MAIL obrigatório — sem enviar e-mail.

Regras:
1. Busca empresas (várias rodadas / queries)
2. 1ª passagem de e-mail → se falhar, 2ª passagem (Playwright + DDG)
3. Sem e-mail → DESCARTA e continua buscando mais candidatos
4. NÃO encerra o nicho zerado/incompleto: repete até atingir N com e-mail
   (ou limite de segurança de rodadas/candidatos)
5. Só grava no banco quem tem e-mail
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import SearchContext
from app.infrastructure.database.models import (
    Campaign,
    CampaignItem,
    CampaignStatus,
    Company,
    Contact,
    ItemStatus,
)
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.providers.email_enrichment import has_valid_email, require_email
from app.providers.http_tools import web_search
from app.providers.registry import get_provider_registry
from app.providers.scraper import extract_emails

logger = get_logger(__name__)

# niche, queries alternativas (rotação até preencher meta)
NICHES: list[tuple[str, list[str], str, str]] = [
    (
        "advogado",
        [
            "escritório advocacia",
            "advogados associados",
            "sociedade de advogados",
            "advogado trabalhista",
            "advogado empresarial",
            "escritório de advocacia site contato",
        ],
        "Brasil",
        "",
    ),
    (
        "agencia_marketing",
        [
            "agência de marketing digital",
            "agência publicidade",
            "marketing digital empresa",
            "agência performance google ads",
            "agência de comunicação",
            "social media agência",
        ],
        "Brasil",
        "",
    ),
    (
        "empresa_ti",
        [
            "empresa de software desenvolvimento",
            "software house",
            "fábrica de software",
            "desenvolvimento de sistemas",
            "empresa de tecnologia ti",
            "consultoria em ti",
        ],
        "Brasil",
        "",
    ),
    (
        "prestador_servico",
        [
            "contabilidade escritório contábil",
            "escritório de contabilidade",
            "prestador de serviços empresariais",
            "consultoria empresarial",
            "escritório contábil site contato",
            "empresa de serviços administrativos",
        ],
        "Brasil",
        "",
    ),
    (
        "grupo_midiatico",
        [
            "jornal portal de notícias",
            "portal de notícias",
            "jornal regional",
            "grupo de mídia comunicação",
            "rádio jornal site",
            "site de notícias",
        ],
        "Brasil",
        "",
    ),
    (
        "politico",
        [
            "deputado federal gabinete e-mail",
            "deputado estadual contato email",
            "senador gabinete contato",
            "vereador contato email",
            "partido político diretório contato",
            "gabinete parlamentar email",
        ],
        "Brasil",
        "",
    ),
]


def _query_variants(base_queries: list[str], city: str, state: str, round_idx: int) -> list[str]:
    """Gera queries extras por rodada (cidade, estado, página)."""
    from app.domain.cities import search_location

    loc = search_location(city, state)
    extras = []
    for q in base_queries:
        extras.append(f"{q} {loc}")
        extras.append(f"{q} {loc} contato email")
        extras.append(f"{q} {loc} site")
    # rotate start by round
    start = (round_idx * 2) % max(len(extras), 1)
    return extras[start:] + extras[:start]


async def seed_providers(session) -> None:
    from app.services.campaign_service import CampaignService

    svc = CampaignService(session)
    n = await svc.seed_providers()
    if n:
        logger.info("providers_seeded", count=n)


async def _fetch_candidates(
    provider,
    *,
    queries: list[str],
    city: str,
    state: str,
    per_query: int,
    seen: set[str],
) -> list[ProviderResult]:
    """Busca em várias queries + web_search de reforço; dedup por seen."""
    batch: list[ProviderResult] = []

    for q in queries:
        ctx = SearchContext(query=q, city=city, state=state, max_results=per_query)
        try:
            found = await provider.search_companies(ctx)
        except Exception as exc:
            logger.warning("search_failed", query=q, error=str(exc))
            found = []

        # reforço free web se provider trouxe pouco
        if len(found) < per_query // 2:
            try:
                raw = await web_search(
                    f"{q} {city} {state}",
                    num=per_query,
                    search_type="search",
                    city=city,
                    state=state,
                )
                for item in raw:
                    title = item.get("title") or item.get("name") or ""
                    link = item.get("link") or item.get("website") or ""
                    snippet = item.get("snippet") or ""
                    emails = extract_emails(snippet)
                    found.append(
                        ProviderResult(
                            company_name=title.split(" - ")[0].split(" | ")[0].strip(),
                            website=link,
                            email=emails[0] if emails else "",
                            city=city,
                            state=state,
                            segment=provider.niche,
                            source=item.get("source") or "duckduckgo",
                            extra={"snippet": snippet, "raw": item},
                        )
                    )
            except Exception as exc:
                logger.warning("web_search_boost_failed", error=str(exc))

        for c in found:
            if not c.is_valid_company():
                continue
            key = c.normalize_key()
            if key in seen:
                continue
            # também dedup por website host
            if c.website:
                host = c.website.lower().rstrip("/")
                if host in seen:
                    continue
                seen.add(host)
            seen.add(key)
            batch.append(c)

        await asyncio.sleep(0.15)  # gentileza rate-limit

    return batch


async def _save_lead(
    session,
    *,
    campaign: Campaign,
    provider,
    kept: ProviderResult,
    city: str,
    state: str,
    do_crm: bool,
) -> tuple[bool, bool]:
    """Persiste lead com e-mail + sync CRM (Account/Contact/Lead). Returns (saved, crm_ok)."""
    website = kept.website or None
    website_host = None
    if website:
        from urllib.parse import urlparse

        host = urlparse(website if "://" in website else f"https://{website}").netloc
        website_host = (host or website)[:191]

    # evita duplicar e-mail já salvo nesta campanha / banco
    email_l = kept.email.strip().lower()
    existing_email = (
        await session.execute(select(Contact).where(Contact.email == email_l).limit(1))
    ).scalar_one_or_none()
    if existing_email:
        logger.info("skip_duplicate_email", email=email_l)
        return False, False

    q = select(Company).where(Company.name == (kept.company_name or "Sem nome")[:191])
    if website_host:
        q = q.where(Company.website_host == website_host)
    existing = (await session.execute(q.limit(1))).scalar_one_or_none()

    if existing:
        db_company = existing
        db_company.email = kept.email
        if website and not db_company.website:
            db_company.website = website
    else:
        db_company = Company(
            id=uuid4().hex,
            name=(kept.company_name or "Sem nome")[:191],
            website=website,
            website_host=website_host,
            phone=kept.phone or None,
            email=kept.email,
            city=kept.city or city or None,
            state=kept.state or state or None,
            segment=provider.niche,
            source=kept.source or "free_search",
            extra=kept.extra,
        )
        session.add(db_company)
        await session.flush()

    db_contact = Contact(
        id=uuid4().hex,
        company_id=db_company.id,
        name=(kept.contact_name or kept.company_name or "Contato")[:255],
        email=kept.email,
        phone=kept.phone or None,
        source=kept.source or None,
        extra=kept.extra,
    )
    session.add(db_contact)
    await session.flush()

    # --- CRM obrigatório na coleta (Lead + Contact + Account) ---
    crm_ok = False
    crm_company_id = crm_contact_id = crm_lead_id = None
    if do_crm:
        from app.infrastructure.crm.sync import CRMSyncService

        sync = CRMSyncService()
        sync_result = await sync.sync_prospect(
            company_name=db_company.name,
            contact_name=db_contact.name,
            email=db_contact.email or "",
            phone=db_contact.phone or "",
            website=db_company.website or "",
            city=db_company.city or "",
            state=db_company.state or "",
            niche=provider.niche,
            description=f"LG Prospector | {provider.niche} | campanha {campaign.id}",
        )
        crm_company_id = sync_result.account_id
        crm_contact_id = sync_result.contact_id
        crm_lead_id = sync_result.lead_id
        if crm_company_id:
            db_company.crm_id = crm_company_id
        if crm_contact_id:
            db_contact.crm_id = crm_contact_id
        crm_ok = sync_result.ok
        if not crm_ok:
            logger.warning(
                "crm_incomplete",
                company=db_company.name,
                email=db_contact.email,
                errors=sync_result.errors,
                account=crm_company_id,
                contact=crm_contact_id,
                lead=crm_lead_id,
            )

    session.add(
        CampaignItem(
            id=uuid4().hex,
            campaign_id=campaign.id,
            company_id=db_company.id,
            contact_id=db_contact.id,
            status=ItemStatus.CRM_CREATED.value if crm_ok else ItemStatus.EMAIL_FOUND.value,
            source=db_company.source,
            raw_data={
                "company_name": db_company.name,
                "website": db_company.website,
                "email": db_contact.email,
                "phone": db_contact.phone,
                "city": db_company.city,
                "state": db_company.state,
                "crm": {
                    "account_id": crm_company_id,
                    "contact_id": crm_contact_id,
                    "lead_id": crm_lead_id,
                },
            },
            crm_company_id=crm_company_id,
            crm_contact_id=crm_contact_id,
            crm_lead_id=crm_lead_id,
        )
    )
    await session.flush()
    logger.info(
        "lead_saved_with_email",
        niche=provider.niche,
        company=db_company.name,
        email=db_contact.email,
        crm_lead=crm_lead_id,
        crm_contact=crm_contact_id,
    )
    return True, crm_ok


async def prospect_niche(
    session,
    *,
    niche: str,
    queries: list[str],
    city: str,
    state: str,
    max_results: int,
    do_crm: bool,
    max_rounds: int = 12,
    max_candidates: int = 200,
) -> dict:
    """
    Loop até saved == max_results.
    A cada rodada: novas queries, busca, enriquece e-mail, descarta sem e-mail, grava com e-mail.
    """
    registry = get_provider_registry()
    provider = registry.resolve(niche)

    campaign = Campaign(
        id=uuid4().hex,
        name=f"Prospecção {provider.niche} — {city}/{state}",
        niche=provider.niche,
        provider=provider.code,
        query=queries[0] if queries else provider.niche,
        city=city or None,
        state=state or None,
        max_results=max_results,
        status=CampaignStatus.RUNNING.value,
        config={
            "skip_email": True,
            "require_email": True,
            "fill_until_target": True,
        },
        started_at=datetime.now(timezone.utc),
    )
    session.add(campaign)
    await session.flush()

    seen: set[str] = set()
    saved = 0
    discarded = 0
    crm_ok = 0
    candidates_total = 0
    round_idx = 0

    while saved < max_results and round_idx < max_rounds and candidates_total < max_candidates:
        round_idx += 1
        need = max_results - saved
        # busca generosa por rodada
        per_query = max(8, need * 3)
        variants = _query_variants(queries, city, state, round_idx)
        # poucas queries/rodada + backoff se fontes free estiverem saturadas
        slice_q = variants[: min(4, len(variants))]

        logger.info(
            "search_round",
            niche=provider.niche,
            round=round_idx,
            need=need,
            queries=len(slice_q),
            saved=saved,
        )
        print(
            f"  · rodada {round_idx}: faltam {need}, buscando…",
            flush=True,
        )

        batch = await _fetch_candidates(
            provider,
            queries=slice_q,
            city=city,
            state=state,
            per_query=per_query,
            seen=seen,
        )
        candidates_total += len(batch)
        if not batch:
            logger.warning("empty_batch", niche=provider.niche, round=round_idx)
            await asyncio.sleep(1.5)
            continue

        for company in batch:
            if saved >= max_results:
                break
            if candidates_total > max_candidates and saved < max_results:
                # ainda processa o batch atual mas para depois
                pass

            base = company.model_copy(deep=True)
            if not base.contact_name:
                base.contact_name = (
                    f"Contato {base.company_name}" if base.company_name else "Contato"
                )

            kept = await require_email(base, deep=True)
            if kept is None or not has_valid_email(kept.email):
                discarded += 1
                logger.info("discarded", company=company.company_name, reason="no_email")
                continue

            ok, crm = await _save_lead(
                session,
                campaign=campaign,
                provider=provider,
                kept=kept,
                city=city,
                state=state,
                do_crm=do_crm,
            )
            if ok:
                saved += 1
                if crm:
                    crm_ok += 1
                print(
                    f"    ✓ [{saved}/{max_results}] {kept.company_name[:50]} <{kept.email}>",
                    flush=True,
                )
            await session.commit()

        if saved < max_results:
            await asyncio.sleep(1.0)

    if saved < max_results:
        campaign.status = CampaignStatus.FAILED.value
        campaign.error_message = (
            f"Meta incompleta: {saved}/{max_results} com e-mail "
            f"(rodadas={round_idx}, candidatos={candidates_total}, descartados={discarded})"
        )
        logger.error(
            "niche_incomplete",
            niche=provider.niche,
            saved=saved,
            target=max_results,
            rounds=round_idx,
        )
    else:
        campaign.status = CampaignStatus.COMPLETED.value
        campaign.error_message = None

    campaign.finished_at = datetime.now(timezone.utc)
    await session.commit()

    return {
        "niche": provider.niche,
        "campaign_id": campaign.id,
        "candidates": candidates_total,
        "saved": saved,
        "discarded_no_email": discarded,
        "crm_ok": crm_ok,
        "target": max_results,
        "rounds": round_idx,
        "complete": saved >= max_results,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--max", type=int, default=5, help="leads COM e-mail por nicho")
    parser.add_argument(
        "--no-crm",
        action="store_true",
        help="NÃO sincronizar EspoCRM (por padrão CRM=SIM: Account+Contact+Lead)",
    )
    parser.add_argument("--city", default="Brasil", help="Cidade (default: Brasil = todo o país)")
    parser.add_argument("--state", default="", help="UF (vazio = nacional)")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=200)
    args = parser.parse_args()
    args.crm = not args.no_crm

    setup_logging()
    get_settings.cache_clear()
    reset_engine()
    await init_db()
    factory = async_session_factory()

    async with factory() as session:
        await seed_providers(session)
        await session.commit()

    print("=" * 72)
    print(f"Prospecção — meta {args.max}/nicho COM e-mail | envio=NÃO")
    print(f"CRM Espo: {'SIM (Account+Contact+Lead)' if args.crm else 'NÃO'}")
    print("Loop: descarta sem e-mail e CONTINUA até preencher a meta")
    print(f"Local: {args.city}/{args.state} | backend={get_settings().search_backend}")
    print(f"Limites segurança: rounds≤{args.max_rounds} candidates≤{args.max_candidates}")
    print("=" * 72)

    summary = []
    for niche, queries, dcity, dstate in NICHES:
        city, state = args.city or dcity, args.state or dstate
        print(f"\n→ {niche} | {city}/{state}", flush=True)
        try:
            async with factory() as session:
                result = await prospect_niche(
                    session,
                    niche=niche,
                    queries=queries,
                    city=city,
                    state=state,
                    max_results=args.max,
                    do_crm=args.crm,
                    max_rounds=args.max_rounds,
                    max_candidates=args.max_candidates,
                )
            summary.append(result)
            flag = "OK" if result["complete"] else "INCOMPLETO"
            print(
                f"  {flag} saved={result['saved']}/{result['target']} "
                f"discarded={result['discarded_no_email']} "
                f"rounds={result['rounds']} candidates={result['candidates']}",
                flush=True,
            )
        except Exception as exc:
            logger.exception("niche_failed", niche=niche)
            summary.append({"niche": niche, "error": str(exc), "saved": 0, "complete": False})
            print(f"  FAIL {exc}", flush=True)

    print("\n" + "=" * 72)
    total = sum(s.get("saved", 0) for s in summary)
    disc = sum(s.get("discarded_no_email", 0) for s in summary)
    incomplete = [s for s in summary if not s.get("complete") and not s.get("error")]
    print(f"TOTAL com e-mail: {total} | descartados: {disc} | e-mails enviados: 0")
    for s in summary:
        if s.get("error"):
            print(f"  ✗ {s['niche']}: {s['error']}")
        else:
            mark = "✓" if s.get("complete") else "⚠"
            print(
                f"  {mark} {s['niche']:20} com_email={s['saved']}/{s['target']} "
                f"descartados={s['discarded_no_email']} rounds={s.get('rounds')}"
            )
    print("=" * 72)
    if incomplete:
        print(f"ATENÇÃO: {len(incomplete)} nicho(s) não atingiram a meta (limite de segurança).")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
