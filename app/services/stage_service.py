"""Orquestrador de etapas da campanha.

Etapas independentes (podem ser disparadas via API):
  1. discover — busca empresas
  2. enrich   — e-mail do domínio do site
  3. crm      — Account + Contact + Lead no Espo
  4. dispatch — envio SMTP com template HTML (rate-limited)

Cada etapa avança current_stage e só processa itens no estado certo.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import SearchContext
from app.domain.stages import CampaignStage, ItemStageStatus
from app.infrastructure.crm.sync import CRMSyncService
from app.infrastructure.database.models import (
    Activity,
    Campaign,
    CampaignItem,
    CampaignStatus,
    Company,
    Contact,
    EmailRecord,
    ItemStatus,
)
from app.infrastructure.email.smtp import SMTPService
from app.infrastructure.email.templates import TemplateSelector
from app.infrastructure.redis.rate_limit import RateLimiter
from app.providers.domain_email import extract_registrable_domain
from app.providers.email_enrichment import has_valid_email, require_email
from app.providers.registry import get_provider_registry

logger = get_logger(__name__)


class StageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rate_limiter = RateLimiter(key_prefix="dispatch", max_requests=30, window_seconds=60)

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        result = await self.session.execute(
            select(Campaign)
            .where(Campaign.id == campaign_id)
            .options(
                selectinload(Campaign.items).selectinload(CampaignItem.company),
                selectinload(Campaign.items).selectinload(CampaignItem.contact),
            )
        )
        return result.scalar_one_or_none()

    async def run_stage(self, campaign_id: str, stage: str) -> dict[str, Any]:
        stage = stage.lower().strip()
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")

        handlers = {
            CampaignStage.DISCOVER.value: self.stage_discover,
            CampaignStage.ENRICH.value: self.stage_enrich,
            CampaignStage.CRM.value: self.stage_crm,
            CampaignStage.DISPATCH.value: self.stage_dispatch,
        }
        if stage not in handlers:
            raise ValueError(
                f"Etapa inválida: {stage}. Use: {', '.join(handlers)}"
            )

        campaign.status = CampaignStatus.RUNNING.value
        if not campaign.started_at:
            campaign.started_at = datetime.now(timezone.utc)
        await self.session.flush()

        result = await handlers[stage](campaign)
        await self.session.commit()
        return result

    # ------------------------------------------------------------------
    # 1) DISCOVER
    # ------------------------------------------------------------------
    async def stage_discover(self, campaign: Campaign) -> dict[str, Any]:
        """Busca empresas e cria items em stage=discovered (sem e-mail ainda)."""
        from app.core.config import get_settings

        settings = get_settings()
        registry = get_provider_registry()
        provider = registry.resolve(campaign.niche)
        # overfetch moderado — menos SERP = menos carga
        factor = max(2, int(settings.discover_overfetch_factor))
        fetch_n = max(campaign.max_results * factor, campaign.max_results + 8)
        cfg = dict(campaign.config or {})
        round_idx = int(cfg.get("discover_round", 0))
        base_q = (campaign.query or "").strip()
        # rotaciona query entre rodadas para não repetir os mesmos SERP
        query_variants = [
            base_q,
            f"{base_q} {campaign.city or ''}".strip(),
            f"{base_q} empresa site contato".strip(),
            f"{base_q} {campaign.city or ''} {campaign.state or ''} contatos".strip(),
            f"empresa {base_q} {campaign.city or ''}".strip(),
        ]
        q = query_variants[round_idx % len(query_variants)] or base_q
        ctx = SearchContext(
            query=q,
            city=campaign.city or "",
            state=campaign.state or "",
            max_results=fetch_n,
        )
        found = await provider.search_companies(ctx)

        # filtro LLM opcional (serial, 1 por vez) — default OFF
        if settings.hunt_use_llm and found:
            from app.infrastructure.llm.client import score_company_candidate

            filtered = []
            for pr in found:
                score = await score_company_candidate(
                    name=pr.company_name or "",
                    website=pr.website or "",
                    snippet=str((pr.extra or {}).get("snippet") or ""),
                    niche=campaign.niche,
                    city=campaign.city or "",
                )
                if score.get("keep", True):
                    # aplica nome limpo quando o modelo devolver
                    cn = (score.get("clean_name") or "").strip()
                    if cn and len(cn) > 2:
                        pr.company_name = cn[:191]
                    extra = dict(pr.extra or {})
                    extra["llm_score"] = {
                        "score": score.get("score"),
                        "reason": score.get("reason"),
                        "confidence": score.get("confidence"),
                    }
                    pr.extra = extra
                    filtered.append(pr)
                else:
                    logger.info(
                        "discover_llm_discard",
                        name=pr.company_name,
                        reason=score.get("reason"),
                        score=score.get("score"),
                    )
            found = filtered

        # já existentes nesta campanha (domínio / nome)
        existing_hosts: set[str] = set()
        existing_names: set[str] = set()
        prev_items = (
            await self.session.execute(
                select(CampaignItem)
                .where(CampaignItem.campaign_id == campaign.id)
                .options(selectinload(CampaignItem.company))
            )
        ).scalars().all()
        for it in prev_items:
            if it.company_domain:
                existing_hosts.add(it.company_domain.lower())
            if it.company:
                existing_names.add((it.company.name or "").strip().lower())
            raw = it.raw_data or {}
            n = (raw.get("company_name") or "").strip().lower()
            if n:
                existing_names.add(n)

        # já existentes globalmente (outras campanhas/ciclos) — a constraint
        # uq_company_name_city_host é da tabela inteira, não só desta campanha
        # (o "escada de cidades" cria uma campanha nova por cidade/nicho/ciclo,
        # então sem isso a mesma empresa colide de novo a cada rodada)
        global_rows = (
            await self.session.execute(
                select(Company.name, Company.website_host).where(
                    Company.city == (campaign.city or "")
                )
            )
        ).all()
        for g_name, g_host in global_rows:
            if g_name:
                existing_names.add(g_name.strip().lower())
            if g_host:
                existing_hosts.add(g_host.strip().lower())

        seen: set[str] = set()
        created = 0
        for pr in found:
            pr.segment = pr.segment or campaign.niche
            if not pr.is_valid_company():
                continue
            key = pr.normalize_key()
            if key in seen:
                continue
            seen.add(key)

            domain = extract_registrable_domain(pr.website or "")
            name_l = (pr.company_name or "").strip().lower()
            if domain and domain.lower() in existing_hosts:
                continue
            if name_l and name_l in existing_names:
                continue

            website_host = domain or None

            company = Company(
                id=uuid4().hex,
                name=(pr.company_name or "Sem nome")[:191],
                website=pr.website or None,
                website_host=website_host,
                phone=pr.phone or None,
                email=None,  # só após enrich
                city=pr.city or campaign.city,
                state=pr.state or campaign.state,
                segment=campaign.niche,
                source=pr.source or "discover",
                extra=pr.extra,
            )
            self.session.add(company)
            try:
                async with self.session.begin_nested():
                    await self.session.flush()
            except IntegrityError:
                self.session.expunge(company)
                logger.info(
                    "discover_duplicate_skipped",
                    name=company.name,
                    city=company.city,
                    host=website_host,
                )
                if website_host:
                    existing_hosts.add(website_host.lower())
                if name_l:
                    existing_names.add(name_l)
                continue

            item = CampaignItem(
                id=uuid4().hex,
                campaign_id=campaign.id,
                company_id=company.id,
                status=ItemStatus.SEARCHING.value,
                stage=ItemStageStatus.DISCOVERED.value,
                company_domain=domain or None,
                source=pr.source,
                raw_data=pr.model_dump(),
            )
            self.session.add(item)
            created += 1
            if domain:
                existing_hosts.add(domain.lower())
            if name_l:
                existing_names.add(name_l)

        cfg["discover_round"] = round_idx + 1
        campaign.config = cfg
        campaign.current_stage = CampaignStage.DISCOVER.value
        campaign.provider = provider.code
        await self._log(
            campaign.id,
            "stage_discover",
            f"encontradas={created} round={round_idx} q={q!r} (alvo_final={campaign.max_results})",
        )
        await self.session.flush()
        logger.info("stage_discover_done", campaign_id=campaign.id, created=created, round=round_idx)
        return {
            "stage": "discover",
            "companies_found": created,
            "round": round_idx,
            "query_used": q,
            "next": CampaignStage.ENRICH.value,
        }

    # ------------------------------------------------------------------
    # 2) ENRICH — e-mail do domínio
    # ------------------------------------------------------------------
    async def stage_enrich(self, campaign: Campaign) -> dict[str, Any]:
        """Para cada item discovered: multi-pass e-mail do domínio. Sem e-mail → discarded.

        Processa 1 a 1 com pausa entre leads (não sobrecarrega CPU/rede/LLM).
        """
        from app.core.config import get_settings

        settings = get_settings()
        pause = max(0.0, float(settings.enrich_batch_pause_seconds))

        items = await self._items_in_stage(
            campaign.id, ItemStageStatus.DISCOVERED.value
        )
        enriched = 0
        discarded = 0
        target = campaign.max_results

        for item in items:
            if enriched >= target:
                break
            company = item.company
            if not company:
                item.stage = ItemStageStatus.DISCARDED.value
                item.status = ItemStatus.SKIPPED.value
                discarded += 1
                continue

            # pré-filtro barato (sem scrape/LLM)
            pr = ProviderResult(
                company_name=company.name,
                website=company.website or "",
                phone=company.phone or "",
                city=company.city or "",
                state=company.state or "",
                segment=company.segment or campaign.niche,
                source=company.source or "",
                extra=company.extra or (item.raw_data or {}),
            )
            if not pr.is_valid_company():
                item.stage = ItemStageStatus.DISCARDED.value
                item.status = ItemStatus.SKIPPED.value
                item.error_message = "candidato inválido/lixo"
                discarded += 1
                logger.info("enrich_prefilter_discard", company=company.name)
                continue

            domain = item.company_domain or extract_registrable_domain(company.website or "")
            require_domain = bool(domain)

            kept = await require_email(pr, deep=True, require_domain=require_domain)
            if not kept or not has_valid_email(kept.email):
                item.stage = ItemStageStatus.DISCARDED.value
                item.status = ItemStatus.SKIPPED.value
                item.error_message = "sem e-mail do domínio"
                discarded += 1
                logger.info("enrich_discarded", company=company.name, domain=domain)
                if pause:
                    await asyncio.sleep(pause)
                continue

            company.email = kept.email
            if kept.phone:
                company.phone = kept.phone
            if kept.website and not company.website:
                company.website = kept.website
                company.website_host = extract_registrable_domain(kept.website)

            contact = Contact(
                id=uuid4().hex,
                company_id=company.id,
                name=(kept.contact_name or company.name)[:255],
                email=kept.email,
                phone=kept.phone or company.phone,
                source=kept.source,
                extra=kept.extra,
            )
            self.session.add(contact)
            await self.session.flush()

            item.contact_id = contact.id
            item.stage = ItemStageStatus.ENRICHED.value
            item.status = ItemStatus.EMAIL_FOUND.value
            item.company_domain = domain or extract_registrable_domain(company.website or "")
            item.qualification_notes = f"email domínio={item.company_domain}"
            item.raw_data = {**(item.raw_data or {}), **kept.model_dump()}
            enriched += 1
            if pause:
                await asyncio.sleep(pause)

        # flush para a contagem refletir discarded/enriched desta rodada
        await self.session.flush()

        remaining_discovered = await self._count_stage(
            campaign.id, ItemStageStatus.DISCOVERED.value
        )
        already_ok = await self._count_stages(
            campaign.id,
            [
                ItemStageStatus.ENRICHED.value,
                ItemStageStatus.CRM_SYNCED.value,
                ItemStageStatus.SENT.value,
            ],
        )
        total_good = already_ok  # includes just-enriched (flushed)
        campaign.current_stage = CampaignStage.ENRICH.value

        await self._log(
            campaign.id,
            "stage_enrich",
            f"enriched={enriched} discarded={discarded} target={target} "
            f"total_good={total_good} remaining_discovered={remaining_discovered}",
        )
        return {
            "stage": "enrich",
            "enriched": enriched,
            "discarded": discarded,
            "target": target,
            "total_good": total_good,
            "need_more_discover": total_good < target and remaining_discovered == 0,
            "next": CampaignStage.CRM.value,
        }

    # ------------------------------------------------------------------
    # 3) CRM
    # ------------------------------------------------------------------
    async def stage_crm(self, campaign: Campaign) -> dict[str, Any]:
        items = await self._items_in_stage(campaign.id, ItemStageStatus.ENRICHED.value)
        synced = 0
        failed = 0
        sync = CRMSyncService()

        for item in items:
            company = item.company
            contact = item.contact
            if not company or not contact or not contact.email:
                item.stage = ItemStageStatus.FAILED.value
                item.error_message = "sem contact/email para CRM"
                failed += 1
                continue
            res = await sync.sync_prospect(
                company_name=company.name,
                contact_name=contact.name,
                email=contact.email,
                phone=contact.phone or company.phone or "",
                website=company.website or "",
                city=company.city or "",
                state=company.state or "",
                niche=campaign.niche,
                description=f"LG Prospector | {campaign.niche} | {campaign.id}",
            )
            item.crm_company_id = res.account_id
            item.crm_contact_id = res.contact_id
            item.crm_lead_id = res.lead_id
            if res.account_id:
                company.crm_id = res.account_id
            if res.contact_id:
                contact.crm_id = res.contact_id
            if res.ok:
                item.stage = ItemStageStatus.CRM_SYNCED.value
                item.status = ItemStatus.CRM_CREATED.value
                synced += 1
            else:
                item.stage = ItemStageStatus.FAILED.value
                item.error_message = "; ".join(res.errors or ["crm failed"])
                failed += 1

        campaign.current_stage = CampaignStage.CRM.value
        await self._log(campaign.id, "stage_crm", f"synced={synced} failed={failed}")
        await self.session.flush()
        return {
            "stage": "crm",
            "synced": synced,
            "failed": failed,
            "next": CampaignStage.DISPATCH.value,
        }

    # ------------------------------------------------------------------
    # 4) DISPATCH — envio individual + template por nicho + cooldown
    # ------------------------------------------------------------------
    async def stage_dispatch(
        self,
        campaign: Campaign,
        *,
        dry_run: bool = False,
        max_send: int | None = None,
        delay_seconds: float | None = None,
        cooldown_days: int | None = None,
    ) -> dict[str, Any]:
        """
        Envia e-mail individual para cada lead crm_synced (template do nicho).

        Cooldown: não reenvia para o mesmo endereço se já houve envio
        (sent/dry_run) nos últimos `cooldown_days` dias (default 4).
        """
        from app.core.config import get_settings
        from datetime import timedelta

        settings = get_settings()
        if delay_seconds is None:
            delay_seconds = float(settings.dispatch_delay_seconds)
        if cooldown_days is None:
            cooldown_days = int(settings.email_cooldown_days)

        cfg = campaign.config or {}
        skip = bool(cfg.get("skip_email", False))
        if skip:
            campaign.current_stage = CampaignStage.DONE.value
            campaign.status = CampaignStatus.COMPLETED.value
            campaign.finished_at = datetime.now(timezone.utc)
            await self._log(campaign.id, "stage_dispatch", "skip_email=true")
            await self.session.flush()
            return {"stage": "dispatch", "sent": 0, "skipped": True}

        # aceita crm_synced; se require_crm false, também enriched
        stages = [ItemStageStatus.CRM_SYNCED.value]
        if cfg.get("dispatch_without_crm"):
            stages.append(ItemStageStatus.ENRICHED.value)

        result = await self.session.execute(
            select(CampaignItem)
            .where(
                CampaignItem.campaign_id == campaign.id,
                CampaignItem.stage.in_(stages),
            )
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
            )
        )
        items = list(result.scalars().all())
        if max_send is not None:
            items = items[:max_send]

        selector = TemplateSelector()
        smtp = SMTPService()
        template_name, html, content_hash = selector.load(campaign.niche)
        subject_base = selector.subject_for(campaign.niche)

        sent = 0
        failed = 0
        cooldown_skipped = 0
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))

        # e-mails já enviados nesta rodada (evita duplicata no mesmo batch)
        batch_sent: set[str] = set()

        for item in items:
            contact = item.contact
            company = item.company
            if not contact or not contact.email:
                item.stage = ItemStageStatus.FAILED.value
                item.error_message = "sem e-mail no contato"
                failed += 1
                continue

            to_addr = contact.email.strip().lower()

            # cooldown global por endereço (qualquer campanha)
            if to_addr in batch_sent or await self._email_in_cooldown(
                to_addr, contact_id=contact.id, since=cooldown_cutoff
            ):
                cooldown_skipped += 1
                item.error_message = f"cooldown_{cooldown_days}d"
                # mantém crm_synced — pode tentar de novo depois do prazo
                logger.info(
                    "dispatch_cooldown_skip",
                    to=to_addr,
                    days=cooldown_days,
                    company=company.name if company else None,
                )
                await self._log(
                    campaign.id,
                    "dispatch_cooldown",
                    f"skip {to_addr} (enviado nos últimos {cooldown_days}d)",
                )
                continue

            # rate limit global de disparo
            allowed = await self.rate_limiter.acquire("smtp")
            if not allowed:
                await asyncio.sleep(2.0)
                allowed = await self.rate_limiter.acquire("smtp")
            if not allowed:
                item.error_message = "rate_limit"
                failed += 1
                continue

            # assunto fixo do nicho — sem nome do contato/empresa
            subject = subject_base

            item.stage = ItemStageStatus.QUEUED.value
            await self.session.flush()

            send_result = await smtp.send_html(
                to=contact.email,
                subject=subject,
                html_body=html,
                dry_run=dry_run,
            )
            status = send_result.get("status")
            msg_id = send_result.get("message_id")

            self.session.add(
                EmailRecord(
                    id=uuid4().hex,
                    contact_id=contact.id,
                    campaign_item_id=item.id,
                    to_address=contact.email,
                    from_address=smtp.from_addr,
                    subject=subject,
                    template_name=template_name,
                    body_hash=content_hash,
                    status=status or "failed",
                    message_id=msg_id,
                    error_message=send_result.get("error"),
                    sent_at=datetime.now(timezone.utc)
                    if status in {"sent", "dry_run"}
                    else None,
                )
            )

            if status in {"sent", "dry_run"}:
                item.stage = ItemStageStatus.SENT.value
                item.status = ItemStatus.EMAIL_SENT.value
                item.template_name = template_name
                item.email_sent_at = datetime.now(timezone.utc)
                batch_sent.add(to_addr)
                sent += 1
                logger.info(
                    "dispatch_sent",
                    to=contact.email,
                    subject=subject,
                    status=status,
                    template=template_name,
                )
            else:
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
                item.error_message = send_result.get("error") or "send_failed"
                failed += 1

            await self.session.flush()
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        campaign.current_stage = CampaignStage.DISPATCH.value
        # se todos processados → done (cooldown não conta como pendente de envio imediato)
        pending = await self._count_stages(
            campaign.id,
            [ItemStageStatus.CRM_SYNCED.value, ItemStageStatus.ENRICHED.value, ItemStageStatus.QUEUED.value],
        )
        # itens em cooldown ficam crm_synced; campanha ainda pode ir para done se
        # o restante foi enviado — reprocessar cooldown numa próxima passada
        if pending == cooldown_skipped or pending == 0:
            campaign.current_stage = CampaignStage.DONE.value
            campaign.status = CampaignStatus.COMPLETED.value
            campaign.finished_at = datetime.now(timezone.utc)

        await self._log(
            campaign.id,
            "stage_dispatch",
            f"sent={sent} failed={failed} cooldown_skip={cooldown_skipped} "
            f"dry_run={dry_run} template={template_name} cooldown_days={cooldown_days}",
        )
        await self.session.flush()
        return {
            "stage": "dispatch",
            "sent": sent,
            "failed": failed,
            "cooldown_skipped": cooldown_skipped,
            "cooldown_days": cooldown_days,
            "template": template_name,
            "dry_run": dry_run,
            "next": CampaignStage.DONE.value
            if campaign.current_stage == CampaignStage.DONE.value
            else CampaignStage.DISPATCH.value,
        }

    async def _email_in_cooldown(
        self,
        to_address: str,
        *,
        contact_id: str | None,
        since: datetime,
    ) -> bool:
        """True se já houve envio sent/dry_run para este e-mail (ou contact) desde `since`."""
        from sqlalchemy import func, or_

        addr = (to_address or "").strip().lower()
        if not addr:
            return False

        clauses = [
            EmailRecord.status.in_(["sent", "dry_run"]),
            EmailRecord.sent_at.is_not(None),
            EmailRecord.sent_at >= since,
        ]
        # match por endereço (case-insensitive) ou mesmo contact_id
        identity = [func.lower(EmailRecord.to_address) == addr]
        if contact_id:
            identity.append(EmailRecord.contact_id == contact_id)

        q = (
            select(func.count())
            .select_from(EmailRecord)
            .where(*clauses, or_(*identity))
        )
        n = int((await self.session.execute(q)).scalar() or 0)
        return n > 0

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _items_in_stage(self, campaign_id: str, stage: str) -> list[CampaignItem]:
        result = await self.session.execute(
            select(CampaignItem)
            .where(CampaignItem.campaign_id == campaign_id, CampaignItem.stage == stage)
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
            )
        )
        return list(result.scalars().all())

    async def _count_stage(self, campaign_id: str, stage: str) -> int:
        from sqlalchemy import func

        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CampaignItem)
                    .where(
                        CampaignItem.campaign_id == campaign_id,
                        CampaignItem.stage == stage,
                    )
                )
            ).scalar()
            or 0
        )

    async def _count_stages(self, campaign_id: str, stages: list[str]) -> int:
        from sqlalchemy import func

        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CampaignItem)
                    .where(
                        CampaignItem.campaign_id == campaign_id,
                        CampaignItem.stage.in_(stages),
                    )
                )
            ).scalar()
            or 0
        )

    async def _log(self, campaign_id: str, activity_type: str, description: str) -> None:
        self.session.add(
            Activity(
                id=uuid4().hex,
                campaign_id=campaign_id,
                activity_type=activity_type,
                description=description,
            )
        )

    async def stage_status(self, campaign_id: str) -> dict[str, Any]:
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")
        from sqlalchemy import func

        rows = (
            await self.session.execute(
                select(CampaignItem.stage, func.count())
                .where(CampaignItem.campaign_id == campaign_id)
                .group_by(CampaignItem.stage)
            )
        ).all()
        by_stage = {r[0]: r[1] for r in rows}
        return {
            "campaign_id": campaign.id,
            "name": campaign.name,
            "niche": campaign.niche,
            "status": campaign.status,
            "current_stage": campaign.current_stage or CampaignStage.CREATED.value,
            "max_results": campaign.max_results,
            "items_by_stage": by_stage,
            "stages": [s.value for s in CampaignStage.order()],
        }
