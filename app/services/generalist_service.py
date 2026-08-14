"""Rotina de prospecção GENERALISTA — independente dos nichos.

O hunt só descobre e cadastra. O disparo padrão é o mailman
(`scripts/mailman.py` / MailmanService). Os métodos de envio abaixo
ficam para o mailman (e para `--with-send` legado).

Não altera dispatch/cooldown das campanhas de nicho.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.stages import ItemStageStatus
from app.infrastructure.crm.activity_service import ActivityService
from app.infrastructure.crm.client import CRMClient
from app.infrastructure.database.models import (
    Campaign,
    CampaignItem,
    Company,
    Contact,
    EmailRecord,
    ItemStatus,
)
from app.infrastructure.email.generalist_copy import (
    TEMPLATE_FILE,
    render_html,
    subject_for,
)
from app.infrastructure.email.smtp import (
    SMTPService,
    is_smtp_provider_block,
    pace_after_send,
    smtp_quota_status,
)
from app.providers.geo_email import (
    classify_contact_email,
    email_needs_llm_review,
    is_plausible_lead,
)
from app.providers.generalista import ORIGIN
from app.providers.opportunity import OpportunityReport, analyze_opportunity
from app.providers.public_org import is_public_email, is_public_organ
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService

logger = get_logger(__name__)

NICHE = "generalista"
_EXCLUDED_SEGMENTS = {"politico", "partido"}


class GeneralistService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.camp_svc = CampaignService(session)
        self.stage_svc = StageService(session)
        self.smtp = SMTPService()
        self.crm_activity = ActivityService(CRMClient())

    # ------------------------------------------------------------------
    # orquestração
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        city: str = "",
        state: str = "",
        query: str = "",
        max_new: int = 10,
        max_send_existing: int = 20,
        max_send_followup: int = 20,
        wait_days: int = 4,
        dry_run: bool = False,
        skip_existing: bool = False,
        skip_followup: bool = False,
        skip_discover: bool = False,
        delay_seconds: float | None = None,
        query_round: int = 0,
    ) -> dict[str, Any]:
        settings = get_settings()
        delay = (
            float(settings.dispatch_delay_seconds)
            if delay_seconds is None
            else float(delay_seconds)
        )
        metrics: dict[str, Any] = {
            "origin": ORIGIN,
            "city": city,
            "state": state,
            "dry_run": dry_run,
            "wait_days": wait_days,
        }

        if not skip_existing:
            metrics["etapa1_crm_existente"] = await self.dispatch_existing_crm(
                max_send=max_send_existing,
                dry_run=dry_run,
                delay_seconds=delay,
            )
        else:
            metrics["etapa1_crm_existente"] = {"skipped": True}

        if (metrics.get("etapa1_crm_existente") or {}).get("provider_blocked"):
            metrics["smtp_provider_block"] = True
            return metrics

        if not skip_followup:
            metrics["etapa2_followup"] = await self.dispatch_mature_generalist(
                max_send=max_send_followup,
                wait_days=wait_days,
                dry_run=dry_run,
                delay_seconds=delay,
            )
        else:
            metrics["etapa2_followup"] = {"skipped": True}

        if (metrics.get("etapa2_followup") or {}).get("provider_blocked"):
            metrics["smtp_provider_block"] = True
            return metrics

        if not skip_discover:
            metrics["etapa3_descoberta"] = await self.discover_and_register(
                city=city,
                state=state,
                query=query,
                max_new=max_new,
                query_round=query_round,
            )
        else:
            metrics["etapa3_descoberta"] = {"skipped": True}

        metrics["etapa4"] = (
            "novos leads generalistas aguardam "
            f"{wait_days} dias antes do primeiro envio"
        )
        logger.info("generalista_run_done", **{k: metrics[k] for k in metrics if k != "etapa4"})
        return metrics

    # ------------------------------------------------------------------
    # ETAPA 1 — CRM já existente (sem espera de 4 dias)
    # ------------------------------------------------------------------
    async def dispatch_existing_crm(
        self,
        *,
        max_send: int,
        dry_run: bool,
        delay_seconds: float,
    ) -> dict[str, Any]:
        contacts = await self._eligible_existing_contacts(limit=max_send * 3)
        sent = failed = skipped = 0
        provider_blocked = False
        reasons: Counter[str] = Counter()
        quota_ok, quota_why = await smtp_quota_status(self.session)
        if not quota_ok:
            reasons[quota_why] += 1
            return {
                "candidatos": len(contacts),
                "enviados": 0,
                "falhas": 0,
                "pulados": len(contacts),
                "motivos": dict(reasons),
                "provider_blocked": False,
                "quota_paused": True,
            }
        for ct in contacts:
            if sent + failed >= max_send:
                break
            quota_ok, quota_why = await smtp_quota_status(self.session)
            if not quota_ok:
                reasons[quota_why] += 1
                break
            company = ct.company
            ok, why = await self._can_send(
                ct,
                company,
                require_wait=False,
                only_origin=None,
            )
            if not ok:
                skipped += 1
                reasons[why] += 1
                continue
            report = self._report_for(company)
            status = await self._send_one(
                ct, company, report, dry_run=dry_run, item=None
            )
            if status in {"sent", "dry_run"}:
                sent += 1
            elif status == "blocked":
                provider_blocked = True
                reasons["smtp_provider_block"] += 1
                break
            else:
                failed += 1
                reasons[status or "send_failed"] += 1
            await pace_after_send(delay_seconds)
        return {
            "candidatos": len(contacts),
            "enviados": sent,
            "falhas": failed,
            "pulados": skipped,
            "motivos": dict(reasons),
            "provider_blocked": provider_blocked,
        }

    # ------------------------------------------------------------------
    # ETAPA 2 — generalista maduro (≥ wait_days)
    # ------------------------------------------------------------------
    async def dispatch_mature_generalist(
        self,
        *,
        max_send: int,
        wait_days: int,
        dry_run: bool,
        delay_seconds: float,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, wait_days))
        items = await self._mature_generalist_items(cutoff, limit=max_send * 3)
        sent = failed = skipped = 0
        provider_blocked = False
        reasons: Counter[str] = Counter()
        quota_ok, quota_why = await smtp_quota_status(self.session)
        if not quota_ok:
            reasons[quota_why] += 1
            return {
                "maduros": len(items),
                "enviados": 0,
                "falhas": 0,
                "pulados": len(items),
                "motivos": dict(reasons),
                "provider_blocked": False,
                "quota_paused": True,
            }
        for item in items:
            if sent + failed >= max_send:
                break
            quota_ok, quota_why = await smtp_quota_status(self.session)
            if not quota_ok:
                reasons[quota_why] += 1
                break
            ct = item.contact
            company = item.company
            if not ct or not company:
                skipped += 1
                reasons["sem_contato"] += 1
                continue
            ok, why = await self._can_send(
                ct,
                company,
                require_wait=True,
                wait_cutoff=cutoff,
                only_origin=ORIGIN,
            )
            if not ok:
                skipped += 1
                reasons[why] += 1
                continue
            extra = company.extra or {}
            report = OpportunityReport(
                digital_presence=str(extra.get("digital_presence") or ""),
                opportunities=list(extra.get("opportunities") or []),
                services=list(extra.get("services") or []),
                personalized_line=str(extra.get("personalized_line") or ""),
                flags=list(extra.get("flags") or []),
            )
            if not report.personalized_line:
                report = self._report_for(company)
            status = await self._send_one(
                ct, company, report, dry_run=dry_run, item=item
            )
            if status in {"sent", "dry_run"}:
                sent += 1
                item.stage = ItemStageStatus.SENT.value
                item.status = ItemStatus.EMAIL_SENT.value
                item.template_name = TEMPLATE_FILE
                item.email_sent_at = datetime.now(timezone.utc)
            elif status == "blocked":
                provider_blocked = True
                reasons["smtp_provider_block"] += 1
                item.error_message = "smtp_provider_block"
                await self.session.flush()
                break
            else:
                failed += 1
                reasons[status or "send_failed"] += 1
                item.error_message = status or "send_failed"
            await self.session.flush()
            await pace_after_send(delay_seconds)
        return {
            "maduros": len(items),
            "enviados": sent,
            "falhas": failed,
            "pulados": skipped,
            "motivos": dict(reasons),
            "provider_blocked": provider_blocked,
        }

    # ------------------------------------------------------------------
    # ETAPA 3 — descobrir, analisar, cadastrar (NÃO envia)
    # ------------------------------------------------------------------
    async def discover_and_register(
        self,
        *,
        city: str,
        state: str,
        query: str,
        max_new: int,
        query_round: int = 0,
    ) -> dict[str, Any]:
        from app.domain.cities import pick_query

        q = query or pick_query(NICHE, city, query_round)
        name = f"Generalista {city} {state} {datetime.now().strftime('%m%d-%H%M')}"
        camp = await self.camp_svc.create_campaign(
            name=name,
            niche=NICHE,
            query=q,
            city=city,
            state=state,
            max_results=max_new,
            config={
                "skip_email": True,
                "origin": ORIGIN,
                "generalista": True,
                "query_round": query_round,
                "discover_round": query_round,
            },
            run_async=False,
        )
        discover = await self.stage_svc.run_stage(camp.id, "discover")
        enrich = await self.stage_svc.run_stage(camp.id, "enrich")
        analyzed = await self._analyze_enriched(camp.id)
        crm = await self.stage_svc.run_stage(camp.id, "crm")

        camp = await self.stage_svc.get_campaign(camp.id)
        if camp:
            camp.current_stage = "crm"
            await self.session.flush()

        return {
            "campaign_id": camp.id if camp else None,
            "query": q,
            "pesquisadas": discover.get("companies_found", 0),
            "enriquecidas": enrich.get("enriched", 0),
            "descartadas": enrich.get("discarded", 0),
            "analisadas": analyzed,
            "cadastradas_crm": crm.get("synced", 0),
            "crm_falhas": crm.get("failed", 0),
            "nota": "novos leads NÃO recebem e-mail agora (espera 4 dias)",
        }

    async def _analyze_enriched(self, campaign_id: str) -> int:
        items = (
            await self.session.execute(
                select(CampaignItem)
                .where(
                    CampaignItem.campaign_id == campaign_id,
                    CampaignItem.stage == ItemStageStatus.ENRICHED.value,
                )
                .options(
                    selectinload(CampaignItem.company),
                    selectinload(CampaignItem.contact),
                )
            )
        ).scalars().all()
        n = 0
        now = datetime.now(timezone.utc).isoformat()
        for item in items:
            company = item.company
            if not company:
                continue
            extra = dict(company.extra or {})
            report = analyze_opportunity(
                name=company.name or "",
                website=company.website or "",
                snippet=str(extra.get("snippet") or ""),
                scrape=extra.get("scrape") if isinstance(extra.get("scrape"), dict) else None,
                extra=extra,
            )
            extra.update(report.as_extra())
            extra["origin"] = ORIGIN
            extra["discovered_at"] = extra.get("discovered_at") or now
            extra["crm_description"] = report.crm_description(
                company=company.name or "",
                city=company.city or "",
                origin=ORIGIN,
            )
            company.extra = extra
            company.source = ORIGIN
            company.segment = NICHE
            if item.contact:
                cextra = dict(item.contact.extra or {})
                cextra["origin"] = ORIGIN
                item.contact.extra = cextra
                item.contact.source = ORIGIN
            item.source = ORIGIN
            raw = dict(item.raw_data or {})
            raw.update(report.as_extra())
            raw["origin"] = ORIGIN
            item.raw_data = raw
            item.qualification_notes = (
                f"generalista | {report.digital_presence} | "
                f"{', '.join(report.services)}"
            )[:500]
            n += 1
        await self.session.flush()
        return n

    # ------------------------------------------------------------------
    # envio
    # ------------------------------------------------------------------
    async def _send_one(
        self,
        contact: Contact,
        company: Company | None,
        report: OpportunityReport,
        *,
        dry_run: bool,
        item: CampaignItem | None,
    ) -> str:
        to_addr = (contact.email or "").strip().lower()
        company_name = (company.name if company else contact.name) or "sua empresa"
        subject = subject_for(company_name, seed=(item.id if item else contact.id))
        _, html, content_hash = render_html(
            company_name=company_name,
            report=report,
            personalized_line=report.personalized_line,
        )
        result = await self.smtp.send_html(
            to=to_addr, subject=subject, html_body=html, dry_run=dry_run
        )
        status = result.get("status") or "failed"
        if result.get("provider_blocked") or is_smtp_provider_block(result.get("error")):
            status = "blocked"
        rec_status = "failed" if status == "blocked" else status
        rec = EmailRecord(
            id=uuid4().hex,
            contact_id=contact.id,
            campaign_item_id=item.id if item else None,
            to_address=to_addr,
            from_address=self.smtp.from_addr,
            subject=subject,
            template_name=TEMPLATE_FILE,
            body_hash=content_hash,
            status=rec_status,
            message_id=result.get("message_id"),
            error_message=result.get("error"),
            sent_at=datetime.now(timezone.utc) if status in {"sent", "dry_run"} else None,
        )
        self.session.add(rec)
        extra = dict(contact.extra or {})
        extra["last_generalist_status"] = status
        if status in {"sent", "dry_run"}:
            extra["last_generalist_sent_at"] = datetime.now(timezone.utc).isoformat()
        contact.extra = extra
        parent_id = (company.crm_id if company else "") or contact.crm_id or ""
        if parent_id:
            try:
                await self.crm_activity.log_email_sent(
                    subject,
                    to=to_addr,
                    parent_type="Account" if company and company.crm_id else "Lead",
                    parent_id=parent_id,
                )
            except Exception as exc:
                logger.debug("generalista_crm_activity_fail", error=str(exc))
        await self.session.flush()
        logger.info(
            "generalista_email",
            to=to_addr,
            company=company_name,
            status=status,
            template=TEMPLATE_FILE,
        )
        return status

    def _report_for(self, company: Company | None) -> OpportunityReport:
        if not company:
            return analyze_opportunity(name="")
        extra = company.extra or {}
        if extra.get("personalized_line"):
            return OpportunityReport(
                digital_presence=str(extra.get("digital_presence") or ""),
                opportunities=list(extra.get("opportunities") or []),
                services=list(extra.get("services") or []),
                personalized_line=str(extra.get("personalized_line") or ""),
                flags=list(extra.get("flags") or []),
            )
        return analyze_opportunity(
            name=company.name or "",
            website=company.website or "",
            snippet=str(extra.get("snippet") or ""),
            extra=extra,
        )

    async def _can_send(
        self,
        contact: Contact,
        company: Company | None,
        *,
        require_wait: bool,
        wait_cutoff: datetime | None = None,
        only_origin: str | None,
    ) -> tuple[bool, str]:
        email = (contact.email or "").strip().lower()
        if not email or "@" not in email:
            return False, "sem_email"
        extra_c = contact.extra or {}
        extra_co = (company.extra if company else None) or {}
        if extra_c.get("opt_out") or extra_co.get("opt_out"):
            return False, "opt_out"
        if extra_c.get("negociacao_ativa") or extra_co.get("negociacao_ativa"):
            return False, "negociacao_ativa"
        seg = ((company.segment if company else "") or "").lower()
        if seg in _EXCLUDED_SEGMENTS:
            return False, "segmento_excluido"
        origin = (company.source if company else "") or extra_co.get("origin") or ""
        if only_origin and origin != only_origin:
            return False, "origem_outra"
        if require_wait and wait_cutoff and company and company.created_at:
            created = company.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created > wait_cutoff:
                return False, "aguarda_4_dias"
        # e-mail generalista: julga como negócio BR, não pelo nicho original
        # (ex.: "Especialista INSS" num escritório de advocacia não é o INSS)
        if is_public_email(email, allow_gov_br=True) or is_public_organ(
            name=(company.name if company else "") or contact.name or "",
            website=(company.website if company else "") or "",
            email=email,
            segment=NICHE,
            allow_gov_br=True,
        ):
            return False, "orgao_publico"
        if company and not is_plausible_lead(
            name=company.name or "",
            website=company.website or "",
            email=email,
            snippet=str(extra_co.get("snippet") or ""),
            segment=NICHE,
        ):
            return False, "fora_do_nicho_ou_nacionalidade"
        ok, reason = classify_contact_email(
            email,
            name=contact.name or (company.name if company else "") or "",
            city=(company.city if company else "") or "",
            website=(company.website if company else "") or "",
            segment=NICHE,
        )
        if not ok:
            return False, f"email:{reason}"
        if await self.stage_svc._email_was_bounced(email, contact_id=contact.id):
            return False, "bounce"
        if await self._already_sent_generalist(email, contact.id):
            return False, "ja_enviado_este_template"
        if email_needs_llm_review(email):
            from app.infrastructure.llm.client import score_email_belongs_to_business

            verdict = await score_email_belongs_to_business(
                email=email,
                name=contact.name or (company.name if company else "") or "",
                website=(company.website if company else "") or "",
                city=(company.city if company else "") or "",
                segment=NICHE,
                snippet=str(extra_co.get("snippet") or ""),
            )
            extra_c = dict(contact.extra or {})
            extra_c["llm_email"] = verdict
            contact.extra = extra_c
            if not verdict.get("keep", True):
                return False, f"llm_email:{verdict.get('reason') or 'drop'}"
        return True, "ok"

    async def _already_sent_generalist(self, email: str, contact_id: str | None) -> bool:
        identity = [func.lower(EmailRecord.to_address) == email]
        if contact_id:
            identity.append(EmailRecord.contact_id == contact_id)
        q = (
            select(func.count())
            .select_from(EmailRecord)
            .where(
                EmailRecord.template_name == TEMPLATE_FILE,
                EmailRecord.status.in_(["sent", "dry_run"]),
                or_(*identity),
            )
        )
        n = int((await self.session.execute(q)).scalar() or 0)
        return n > 0

    async def _eligible_existing_contacts(self, *, limit: int) -> list[Contact]:
        """Contatos já no CRM que NÃO são leads generalistas novos.

        Exclui quem já recebeu este template — senão a janela dos 45 mais
        antigos trava e nunca chega no restante da base.
        """
        already = exists(
            select(EmailRecord.id).where(
                EmailRecord.template_name == TEMPLATE_FILE,
                EmailRecord.status.in_(["sent", "dry_run"]),
                or_(
                    EmailRecord.contact_id == Contact.id,
                    func.lower(EmailRecord.to_address) == func.lower(Contact.email),
                ),
            )
        )
        q = (
            select(Contact)
            .join(Company, Contact.company_id == Company.id)
            .options(selectinload(Contact.company))
            .where(
                Contact.email.is_not(None),
                Contact.email != "",
                Contact.crm_id.is_not(None),
                Contact.crm_id != "",
                ~already,
                or_(
                    Company.segment.is_(None),
                    Company.segment.notin_(list(_EXCLUDED_SEGMENTS)),
                ),
                or_(
                    Company.source.is_(None),
                    Company.source != ORIGIN,
                    func.coalesce(Company.source, "") == "",
                ),
            )
            .order_by(Contact.created_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(q)).scalars().unique().all())

    async def _mature_generalist_items(
        self, cutoff: datetime, *, limit: int
    ) -> list[CampaignItem]:
        q = (
            select(CampaignItem)
            .join(Company, CampaignItem.company_id == Company.id)
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.contact),
            )
            .where(
                CampaignItem.stage == ItemStageStatus.CRM_SYNCED.value,
                Company.source == ORIGIN,
                Company.created_at <= cutoff,
            )
            .order_by(Company.created_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(q)).scalars().unique().all())
