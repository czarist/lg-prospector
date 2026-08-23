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
import re
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
from app.infrastructure.email.smtp import (
    SMTPService,
    is_smtp_provider_block,
    pace_after_send,
    smtp_quota_status,
)
from app.infrastructure.email.templates import TemplateSelector
from app.infrastructure.redis.rate_limit import RateLimiter
from app.providers.domain_email import extract_registrable_domain
from app.providers.email_enrichment import has_valid_email, require_email
from app.providers.registry import get_provider_registry

logger = get_logger(__name__)

# sufixos de tipo societário BR — "Advocacia Silva" e "Advocacia Silva Ltda" são
# a mesma empresa pro dedup, mas escapavam da comparação por string exata
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s\-,.]+(ltda\.?|eireli|eirl|epp|s\.?/?a\.?|m\.?e\.?|ei|ss)\.?$", re.IGNORECASE
)


def _normalize_company_name(name: str) -> str:
    n = (name or "").strip().lower()
    prev = None
    while prev != n:
        prev = n
        n = _LEGAL_SUFFIX_RE.sub("", n).strip()
    return n


class StageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rate_limiter = RateLimiter(key_prefix="dispatch")

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

        # já conhecidos — o provider recebe a lista e continua buscando
        # até achar lead NOVO, em vez de reciclar o mesmo SERP
        existing_hosts: set[str] = set()
        existing_names: set[str] = set()
        existing_emails: set[str] = set()
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
                existing_names.add(_normalize_company_name(it.company.name))
            raw = it.raw_data or {}
            n = _normalize_company_name(raw.get("company_name") or "")
            if n:
                existing_names.add(n)
        from app.domain.cities import is_nationwide

        if not is_nationwide(campaign.city or "", campaign.state or ""):
            city_rows = (
                await self.session.execute(
                    select(Company.name, Company.website_host, Company.email).where(
                        Company.city == (campaign.city or "")
                    )
                )
            ).all()
            for g_name, g_host, g_email in city_rows:
                if g_name:
                    existing_names.add(_normalize_company_name(g_name))
                if g_host:
                    existing_hosts.add(g_host.strip().lower())
                if g_email:
                    existing_emails.add(g_email.strip().lower())
        host_rows = (
            await self.session.execute(
                select(Company.website_host).where(Company.website_host.is_not(None))
            )
        ).scalars().all()
        for g_host in host_rows:
            if g_host:
                existing_hosts.add(g_host.strip().lower())

        ctx = SearchContext(
            query=q,
            city=campaign.city or "",
            state=campaign.state or "",
            max_results=fetch_n,
            extra={
                "discover_round": round_idx,
                "query_round": round_idx,
                "exclude_hosts": list(existing_hosts),
                "exclude_names": list(existing_names),
                "exclude_emails": list(existing_emails),
            },
        )
        found = await provider.search_companies(ctx)

        cfg["discover_round"] = round_idx + 1
        campaign.config = cfg
        campaign.current_stage = CampaignStage.DISCOVER.value
        campaign.provider = provider.code

        # fila de revisão: a caçada não chama Qwen nem grava lead
        if settings.review_queue_enabled:
            from app.services.review_service import ReviewService

            queued = await ReviewService(self.session).enqueue_campaign_hits(campaign, found)
            await self._log(
                campaign.id,
                "stage_discover",
                f"enfileiradas={queued} round={round_idx} q={q!r} (alvo_final={campaign.max_results})",
            )
            await self.session.flush()
            logger.info(
                "stage_discover_queued",
                campaign_id=campaign.id,
                queued=queued,
                pool=len(found),
                round=round_idx,
            )
            return {
                "stage": "discover",
                "companies_found": 0,
                "queued": queued,
                "round": round_idx,
                "query_used": q,
                "next": CampaignStage.ENRICH.value,
            }

        created = await self.persist_discovered(
            campaign,
            found,
            existing_hosts=existing_hosts,
            existing_names=existing_names,
            existing_emails=existing_emails,
        )
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
            "queued": 0,
            "round": round_idx,
            "query_used": q,
            "next": CampaignStage.ENRICH.value,
        }

    async def persist_discovered(
        self,
        campaign: Campaign,
        found: list[ProviderResult],
        *,
        existing_hosts: set[str] | None = None,
        existing_names: set[str] | None = None,
        existing_emails: set[str] | None = None,
    ) -> int:
        """Grava Company + item discovered. Chamado pelo reviewer (após Qwen) ou fallback."""
        existing_hosts = set(existing_hosts or [])
        existing_names = set(existing_names or [])
        existing_emails = set(existing_emails or [])
        if not existing_hosts:
            host_rows = (
                await self.session.execute(
                    select(Company.website_host).where(Company.website_host.is_not(None))
                )
            ).scalars().all()
            for g_host in host_rows:
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
            name_l = _normalize_company_name(pr.company_name or "")
            if domain and domain.lower() in existing_hosts:
                continue
            if name_l and name_l in existing_names:
                continue
            pr_email = (pr.email or "").strip().lower()
            if pr_email and pr_email in existing_emails:
                continue

            website_host = domain or None

            # se o provider já achou e-mail (ex.: TSE+web), grava seed p/ enrich
            from app.providers.email_enrichment import has_valid_email
            from app.providers.public_org import is_public_email

            seed_email = (pr.email or "").strip() or None
            allow_gov = (campaign.niche or "").lower() == "generalista"
            if seed_email and (
                not has_valid_email(seed_email)
                or is_public_email(seed_email, allow_gov_br=allow_gov)
            ):
                seed_email = None
            if seed_email:
                from app.providers.geo_email import classify_contact_email

                ok_geo, _reason = classify_contact_email(
                    seed_email,
                    name=pr.contact_name or pr.company_name,
                    city=pr.city or campaign.city or "",
                    party=str((pr.extra or {}).get("tse_partido") or ""),
                    website=pr.website or "",
                    segment=campaign.niche,
                )
                if not ok_geo:
                    seed_email = None

            company = Company(
                id=uuid4().hex,
                name=(pr.company_name or "Sem nome")[:191],
                website=pr.website or None,
                website_host=website_host,
                phone=pr.phone or None,
                email=seed_email,  # seed; enrich confirma / completa
                city=pr.city or campaign.city,
                state=pr.state or campaign.state,
                segment=campaign.niche,
                source=pr.source or "discover",
                extra=pr.extra,
            )
            self.session.add(company)
            dup_name = company.name
            dup_city = company.city
            try:
                async with self.session.begin_nested():
                    await self.session.flush()
            except IntegrityError:
                # o rollback do savepoint já tira a instância da Session
                if company in self.session:
                    try:
                        self.session.expunge(company)
                    except Exception:
                        pass
                logger.info(
                    "discover_duplicate_skipped",
                    name=dup_name,
                    city=dup_city,
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

        await self.session.flush()
        return created

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
            status = await self._enrich_item(item, campaign, pause=pause)
            if status == "enriched":
                enriched += 1
            elif status == "discarded":
                discarded += 1

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

    async def enrich_ready(
        self,
        *,
        niche: str,
        limit: int,
        exclude_campaign_id: str | None = None,
    ) -> dict[str, Any]:
        """Enriquece itens já validados (discovered) de qualquer campanha do nicho.

        A caçada enfileira; o reviewer grava discovered noutro momento.
        O enrich da campanha da vez pode vir vazio — esta drenagem pega o que
        o Qwen já aprovou.
        """
        from app.core.config import get_settings

        settings = get_settings()
        pause = max(0.0, float(settings.enrich_batch_pause_seconds))
        q = (
            select(CampaignItem)
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .where(
                CampaignItem.stage == ItemStageStatus.DISCOVERED.value,
                Campaign.niche == niche,
            )
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
                selectinload(CampaignItem.campaign),
            )
            .limit(max(1, int(limit)))
        )
        if exclude_campaign_id:
            q = q.where(CampaignItem.campaign_id != exclude_campaign_id)
        items = list((await self.session.execute(q)).scalars().all())
        enriched = 0
        discarded = 0
        for item in items:
            camp = item.campaign
            if not camp:
                continue
            status = await self._enrich_item(item, camp, pause=pause)
            if status == "enriched":
                enriched += 1
            elif status == "discarded":
                discarded += 1
        await self.session.flush()
        logger.info("enrich_ready_done", niche=niche, enriched=enriched, discarded=discarded)
        return {"stage": "enrich_ready", "enriched": enriched, "discarded": discarded, "seen": len(items)}

    async def crm_ready(
        self,
        *,
        niche: str,
        limit: int,
        exclude_campaign_id: str | None = None,
    ) -> dict[str, Any]:
        q = (
            select(CampaignItem)
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .where(
                CampaignItem.stage == ItemStageStatus.ENRICHED.value,
                Campaign.niche == niche,
            )
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
                selectinload(CampaignItem.campaign),
            )
            .limit(max(1, int(limit)))
        )
        if exclude_campaign_id:
            q = q.where(CampaignItem.campaign_id != exclude_campaign_id)
        items = list((await self.session.execute(q)).scalars().all())
        synced = 0
        failed = 0
        sync = CRMSyncService()
        for item in items:
            camp = item.campaign
            if not camp:
                continue
            ok = await self._crm_item(item, camp, sync=sync)
            if ok:
                synced += 1
            else:
                failed += 1
        await self.session.flush()
        logger.info("crm_ready_done", niche=niche, synced=synced, failed=failed)
        return {"stage": "crm_ready", "synced": synced, "failed": failed, "seen": len(items)}

    # ------------------------------------------------------------------
    # 3) CRM
    # ------------------------------------------------------------------
    async def stage_crm(self, campaign: Campaign) -> dict[str, Any]:
        items = await self._items_in_stage(campaign.id, ItemStageStatus.ENRICHED.value)
        synced = 0
        failed = 0
        sync = CRMSyncService()

        for item in items:
            if await self._crm_item(item, campaign, sync=sync):
                synced += 1
            else:
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
        # generalista tem rotina própria (4 dias + texto personalizado)
        if (campaign.niche or "").lower() == "generalista" and not cfg.get(
            "force_niche_dispatch"
        ):
            logger.info("dispatch_skip_generalista", campaign_id=campaign.id)
            campaign.current_stage = CampaignStage.CRM.value
            await self._log(
                campaign.id,
                "stage_dispatch",
                "skip: use scripts/hunt_generalista.py (espera 4d + e-mail personalizado)",
            )
            await self.session.flush()
            return {
                "stage": "dispatch",
                "sent": 0,
                "skipped": True,
                "reason": "generalista_own_routine",
            }
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

        sent = 0
        failed = 0
        cooldown_skipped = 0
        quota_skipped = 0
        provider_blocked = False
        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))

        # e-mails já enviados nesta rodada (evita duplicata no mesmo batch)
        batch_sent: set[str] = set()

        quota_ok, quota_why = await smtp_quota_status(self.session)
        if not quota_ok:
            logger.warning("dispatch_quota_pause", reason=quota_why, campaign_id=campaign.id)
            await self._log(campaign.id, "dispatch_quota", f"pausa envio ({quota_why}); busca segue")
            return {
                "stage": "dispatch",
                "sent": 0,
                "failed": 0,
                "cooldown_skipped": 0,
                "quota_skipped": len(items),
                "quota_reason": quota_why,
                "provider_blocked": False,
                "cooldown_days": cooldown_days,
                "template": template_name,
                "dry_run": dry_run,
                "next": CampaignStage.DISPATCH.value,
            }

        for item in items:
            contact = item.contact
            company = item.company
            if not contact or not contact.email:
                item.stage = ItemStageStatus.FAILED.value
                item.error_message = "sem e-mail no contato"
                failed += 1
                continue

            to_addr = contact.email.strip().lower()

            # nunca enviar para domínio de órgão público (.gov / .leg / .jus / MP…)
            from app.providers.public_org import is_public_email, is_public_organ

            if is_public_email(to_addr) or is_public_organ(
                name=(company.name if company else "") or "",
                website=(company.website if company else "") or "",
                email=to_addr,
                segment=campaign.niche,
            ):
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
                item.error_message = "orgao_publico_bloqueado"
                failed += 1
                logger.info("dispatch_public_org_skip", to=to_addr, company=company.name if company else None)
                continue

            from app.providers.geo_email import classify_contact_email

            extra = (company.extra if company else None) or {}
            ok_geo, geo_reason = classify_contact_email(
                to_addr,
                name=(contact.name or (company.name if company else "") or ""),
                city=(company.city if company else "") or "",
                party=str(extra.get("tse_partido") or ""),
                website=(company.website if company else "") or "",
                segment=campaign.niche,
            )
            if not ok_geo:
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
                item.error_message = f"email_implausivel:{geo_reason}"
                failed += 1
                logger.info(
                    "dispatch_implausible_email_skip",
                    to=to_addr,
                    reason=geo_reason,
                    company=company.name if company else None,
                )
                continue

            # nunca reenviar endereço que já bounceou (caixa inexistente etc.)
            if await self._email_was_bounced(to_addr, contact_id=contact.id):
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
                item.error_message = "email_bounced_blacklist"
                failed += 1
                logger.info(
                    "dispatch_bounce_skip",
                    to=to_addr,
                    company=company.name if company else None,
                )
                await self._log(
                    campaign.id,
                    "dispatch_bounce_skip",
                    f"skip {to_addr} (bounce anterior)",
                )
                continue

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

            quota_ok, quota_why = await smtp_quota_status(self.session)
            if not quota_ok:
                quota_skipped += 1
                item.error_message = quota_why
                logger.warning("dispatch_quota_pause", reason=quota_why, to=to_addr)
                break

            # rate limit global de disparo
            allowed = await self.rate_limiter.acquire("smtp")
            if not allowed:
                await asyncio.sleep(2.0)
                allowed = await self.rate_limiter.acquire("smtp")
            if not allowed:
                item.error_message = "rate_limit"
                failed += 1
                continue

            # assunto por nicho (emoji + variação estável por item)
            subject = selector.subject_for(campaign.niche, seed=item.id)

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
            rec_status = "failed" if status == "blocked" else (status or "failed")

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
                    status=rec_status,
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
            elif send_result.get("provider_blocked") or is_smtp_provider_block(
                send_result.get("error")
            ):
                # conta bloqueou o lote — lead volta pra fila, não queima como failed
                item.stage = ItemStageStatus.CRM_SYNCED.value
                item.error_message = "smtp_provider_block"
                provider_blocked = True
                logger.error(
                    "dispatch_provider_block",
                    to=contact.email,
                    error=send_result.get("error"),
                )
                await self._log(
                    campaign.id,
                    "dispatch_provider_block",
                    f"SMTP bloqueou o envio ({send_result.get('error')}); lote interrompido",
                )
                await self.session.flush()
                break
            else:
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
                item.error_message = send_result.get("error") or "send_failed"
                failed += 1

            await self.session.flush()
            await pace_after_send(delay_seconds)

        campaign.current_stage = CampaignStage.DISPATCH.value
        # se todos processados → done (cooldown não conta como pendente de envio imediato)
        pending = await self._count_stages(
            campaign.id,
            [ItemStageStatus.CRM_SYNCED.value, ItemStageStatus.ENRICHED.value, ItemStageStatus.QUEUED.value],
        )
        # itens em cooldown ficam crm_synced; campanha ainda pode ir para done se
        # o restante foi enviado — reprocessar cooldown numa próxima passada
        if not provider_blocked and (pending == cooldown_skipped or pending == 0):
            campaign.current_stage = CampaignStage.DONE.value
            campaign.status = CampaignStatus.COMPLETED.value
            campaign.finished_at = datetime.now(timezone.utc)

        await self._log(
            campaign.id,
            "stage_dispatch",
            f"sent={sent} failed={failed} cooldown_skip={cooldown_skipped} "
            f"quota_skip={quota_skipped} provider_blocked={provider_blocked} "
            f"dry_run={dry_run} template={template_name} cooldown_days={cooldown_days}",
        )
        await self.session.flush()
        return {
            "stage": "dispatch",
            "sent": sent,
            "failed": failed,
            "cooldown_skipped": cooldown_skipped,
            "quota_skipped": quota_skipped,
            "provider_blocked": provider_blocked,
            "cooldown_days": cooldown_days,
            "template": template_name,
            "dry_run": dry_run,
            "next": CampaignStage.DONE.value
            if campaign.current_stage == CampaignStage.DONE.value
            else CampaignStage.DISPATCH.value,
        }

    async def send_one_item(
        self,
        item: CampaignItem,
        *,
        campaign: Campaign | None = None,
        dry_run: bool = False,
        cooldown_days: int | None = None,
        smtp: SMTPService | None = None,
        selector: TemplateSelector | None = None,
        template_name: str | None = None,
        html: str | None = None,
        content_hash: str | None = None,
        batch_sent: set[str] | None = None,
    ) -> dict[str, Any]:
        """Envia o template do nicho para um único item crm_synced.

        Usado pelo mailman (lote global) e reutilizável pelo dispatch da campanha.
        Não marca a campanha como done — só avança o item.
        """
        from datetime import timedelta

        from app.core.config import get_settings
        from app.providers.geo_email import classify_contact_email
        from app.providers.public_org import is_public_email, is_public_organ

        settings = get_settings()
        if cooldown_days is None:
            cooldown_days = int(settings.email_cooldown_days)
        campaign = campaign or item.campaign
        contact = item.contact
        company = item.company
        if not contact or not contact.email:
            item.stage = ItemStageStatus.FAILED.value
            item.error_message = "sem e-mail no contato"
            await self.session.flush()
            return {"outcome": "failed", "reason": "sem_email", "to": ""}

        to_addr = contact.email.strip().lower()
        niche = (campaign.niche if campaign else "") or (company.segment if company else "") or ""

        if is_public_email(to_addr) or is_public_organ(
            name=(company.name if company else "") or "",
            website=(company.website if company else "") or "",
            email=to_addr,
            segment=niche,
        ):
            item.stage = ItemStageStatus.FAILED.value
            item.status = ItemStatus.FAILED.value
            item.error_message = "orgao_publico_bloqueado"
            await self.session.flush()
            return {"outcome": "failed", "reason": "orgao_publico", "to": to_addr}

        extra = (company.extra if company else None) or {}
        ok_geo, geo_reason = classify_contact_email(
            to_addr,
            name=(contact.name or (company.name if company else "") or ""),
            city=(company.city if company else "") or "",
            party=str(extra.get("tse_partido") or ""),
            website=(company.website if company else "") or "",
            segment=niche,
        )
        if not ok_geo:
            item.stage = ItemStageStatus.FAILED.value
            item.status = ItemStatus.FAILED.value
            item.error_message = f"email_implausivel:{geo_reason}"
            await self.session.flush()
            return {"outcome": "failed", "reason": f"email_implausivel:{geo_reason}", "to": to_addr}

        extra_c = contact.extra or {}
        if extra_c.get("opt_out") or extra.get("opt_out"):
            return {"outcome": "skip", "reason": "opt_out", "to": to_addr}
        if extra_c.get("negociacao_ativa") or extra.get("negociacao_ativa"):
            return {"outcome": "skip", "reason": "negociacao_ativa", "to": to_addr}

        if await self._email_was_bounced(to_addr, contact_id=contact.id):
            item.stage = ItemStageStatus.FAILED.value
            item.status = ItemStatus.FAILED.value
            item.error_message = "email_bounced_blacklist"
            await self.session.flush()
            return {"outcome": "failed", "reason": "bounce", "to": to_addr}

        cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, cooldown_days))
        if (batch_sent and to_addr in batch_sent) or await self._email_in_cooldown(
            to_addr, contact_id=contact.id, since=cooldown_cutoff
        ):
            item.error_message = f"cooldown_{cooldown_days}d"
            await self.session.flush()
            return {"outcome": "cooldown", "reason": f"cooldown_{cooldown_days}d", "to": to_addr}

        quota_ok, quota_why = await smtp_quota_status(self.session)
        if not quota_ok:
            item.error_message = quota_why
            await self.session.flush()
            return {"outcome": "quota", "reason": quota_why, "to": to_addr}

        allowed = await self.rate_limiter.acquire("smtp")
        if not allowed:
            await asyncio.sleep(2.0)
            allowed = await self.rate_limiter.acquire("smtp")
        if not allowed:
            item.error_message = "rate_limit"
            await self.session.flush()
            return {"outcome": "failed", "reason": "rate_limit", "to": to_addr}

        selector = selector or TemplateSelector()
        smtp = smtp or SMTPService()
        if not template_name or html is None or content_hash is None:
            template_name, html, content_hash = selector.load(niche)
        subject = selector.subject_for(niche, seed=item.id)

        item.stage = ItemStageStatus.QUEUED.value
        await self.session.flush()

        send_result = await smtp.send_html(
            to=contact.email,
            subject=subject,
            html_body=html,
            dry_run=dry_run,
        )
        status = send_result.get("status")
        rec_status = "failed" if status == "blocked" else (status or "failed")

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
                status=rec_status,
                message_id=send_result.get("message_id"),
                error_message=send_result.get("error"),
                sent_at=datetime.now(timezone.utc) if status in {"sent", "dry_run"} else None,
            )
        )

        if status in {"sent", "dry_run"}:
            item.stage = ItemStageStatus.SENT.value
            item.status = ItemStatus.EMAIL_SENT.value
            item.template_name = template_name
            item.email_sent_at = datetime.now(timezone.utc)
            if batch_sent is not None:
                batch_sent.add(to_addr)
            await self.session.flush()
            logger.info(
                "dispatch_sent",
                to=contact.email,
                subject=subject,
                status=status,
                template=template_name,
                via="mailman_or_item",
            )
            return {
                "outcome": status,
                "reason": "ok",
                "to": to_addr,
                "template": template_name,
                "subject": subject,
            }

        if send_result.get("provider_blocked") or is_smtp_provider_block(send_result.get("error")):
            item.stage = ItemStageStatus.CRM_SYNCED.value
            item.error_message = "smtp_provider_block"
            await self.session.flush()
            logger.error(
                "dispatch_provider_block",
                to=contact.email,
                error=send_result.get("error"),
            )
            return {
                "outcome": "blocked",
                "reason": send_result.get("error") or "smtp_provider_block",
                "to": to_addr,
                "template": template_name,
            }

        item.stage = ItemStageStatus.FAILED.value
        item.status = ItemStatus.FAILED.value
        item.error_message = send_result.get("error") or "send_failed"
        await self.session.flush()
        return {
            "outcome": "failed",
            "reason": send_result.get("error") or "send_failed",
            "to": to_addr,
            "template": template_name,
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

    async def _email_was_bounced(
        self,
        to_address: str,
        *,
        contact_id: str | None,
    ) -> bool:
        """True se já houve bounce permanente para este endereço (qualquer data)."""
        from sqlalchemy import func, or_

        addr = (to_address or "").strip().lower()
        if not addr:
            return False

        identity = [func.lower(EmailRecord.to_address) == addr]
        if contact_id:
            identity.append(EmailRecord.contact_id == contact_id)

        q = (
            select(func.count())
            .select_from(EmailRecord)
            .where(EmailRecord.status == "bounced", or_(*identity))
        )
        n = int((await self.session.execute(q)).scalar() or 0)
        return n > 0

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _enrich_item(
        self, item: CampaignItem, campaign: Campaign, *, pause: float
    ) -> str:
        company = item.company
        if not company:
            item.stage = ItemStageStatus.DISCARDED.value
            item.status = ItemStatus.SKIPPED.value
            return "discarded"

        raw = item.raw_data or {}
        seed_email = (company.email or raw.get("email") or "") or ""
        pr = ProviderResult(
            company_name=company.name,
            website=company.website or "",
            phone=company.phone or "",
            email=seed_email,
            city=company.city or "",
            state=company.state or "",
            segment=company.segment or campaign.niche,
            source=company.source or "",
            contact_name=(raw.get("contact_name") or "") or "",
            extra=company.extra or raw,
        )
        if not pr.is_valid_company():
            item.stage = ItemStageStatus.DISCARDED.value
            item.status = ItemStatus.SKIPPED.value
            item.error_message = "candidato inválido/lixo"
            logger.info("enrich_prefilter_discard", company=company.name)
            return "discarded"

        domain = item.company_domain or extract_registrable_domain(company.website or "")
        niche_l = (campaign.niche or "").lower()
        is_politico = niche_l in {"politico", "partido"}
        is_generalista = niche_l == "generalista"
        require_domain = bool(domain) and not is_politico and not is_generalista
        kept = await require_email(
            pr,
            deep=True,
            require_domain=require_domain,
            allow_free_mail=is_politico or is_generalista or not domain,
        )
        if not kept or not has_valid_email(kept.email):
            item.stage = ItemStageStatus.DISCARDED.value
            item.status = ItemStatus.SKIPPED.value
            item.error_message = "sem e-mail"
            logger.info("enrich_discarded", company=company.name, domain=domain)
            if pause:
                await asyncio.sleep(pause)
            return "discarded"

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
        if pause:
            await asyncio.sleep(pause)
        return "enriched"

    async def _crm_item(
        self,
        item: CampaignItem,
        campaign: Campaign,
        *,
        sync: CRMSyncService | None = None,
    ) -> bool:
        sync = sync or CRMSyncService()
        company = item.company
        contact = item.contact
        if not company or not contact or not contact.email:
            item.stage = ItemStageStatus.FAILED.value
            item.error_message = "sem contact/email para CRM"
            return False
        extra = company.extra or {}
        desc = extra.get("crm_description") or (
            f"LG Prospector | origem={extra.get('origin') or campaign.niche} | {campaign.id}"
        )
        res = await sync.sync_prospect(
            company_name=company.name,
            contact_name=contact.name,
            email=contact.email,
            phone=contact.phone or company.phone or "",
            website=company.website or "",
            city=company.city or "",
            state=company.state or "",
            niche=campaign.niche,
            description=str(desc)[:10000],
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
            return True
        item.stage = ItemStageStatus.FAILED.value
        item.error_message = "; ".join(res.errors or ["crm failed"])
        return False

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
