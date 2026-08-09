"""Service layer de campanhas — orquestra DB + LangGraph + Redis."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.graph.graph import get_campaign_graph
from app.infrastructure.database.models import (
    Activity,
    Campaign,
    CampaignItem,
    CampaignStatus,
    Company,
    Contact,
    GraphRun,
    GraphStateRecord,
    ItemStatus,
    Provider,
)
from app.infrastructure.redis.checkpoint import RedisCheckpointStore
from app.infrastructure.redis.queue import JobQueue
from app.providers.registry import get_provider_registry

logger = get_logger(__name__)


class CampaignService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.queue = JobQueue("lg:campaigns")
        self.checkpoints = RedisCheckpointStore()

    async def create_campaign(
        self,
        *,
        name: str,
        niche: str,
        query: str = "",
        city: str = "",
        state: str = "",
        max_results: int = 20,
        config: dict | None = None,
        run_async: bool = True,
    ) -> Campaign:
        registry = get_provider_registry()
        provider = registry.resolve(niche)
        # niche canônico (partido → politico)

        campaign = Campaign(
            id=uuid4().hex,
            name=name,
            niche=provider.niche,  # sempre canônico (ex.: politico)
            provider=provider.code,
            query=query or None,
            city=city or None,
            state=state or None,
            max_results=max_results,
            status=CampaignStatus.PENDING.value,
            current_stage="created",
            config=config,
        )
        self.session.add(campaign)

        thread_id = f"campaign-{campaign.id}"
        graph_run = GraphRun(
            id=uuid4().hex,
            campaign_id=campaign.id,
            thread_id=thread_id,
            status="pending",
        )
        self.session.add(graph_run)
        await self.session.flush()

        await self.queue.enqueue(
            {
                "type": "run_campaign",
                "campaign_id": campaign.id,
                "thread_id": thread_id,
            }
        )

        if run_async:
            asyncio.create_task(self._run_campaign_safe(campaign.id, thread_id))

        await self.session.commit()
        # Recarrega com relacionamentos para evitar lazy-load assíncrono
        loaded = await self.get_campaign(campaign.id)
        logger.info("campaign_created", campaign_id=campaign.id, niche=provider.niche)
        return loaded or campaign

    async def _run_campaign_safe(self, campaign_id: str, thread_id: str) -> None:
        try:
            # Nova sessão para background task
            from app.infrastructure.database.session import async_session_factory

            factory = async_session_factory()
            async with factory() as session:
                svc = CampaignService(session)
                await svc.run_campaign(campaign_id, thread_id)
                await session.commit()
        except Exception as exc:
            logger.exception("campaign_background_failed", campaign_id=campaign_id, error=str(exc))

    async def run_campaign(self, campaign_id: str, thread_id: str | None = None) -> dict[str, Any]:
        result = await self.session.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        campaign = result.scalar_one_or_none()
        if not campaign:
            raise ValueError(f"Campanha não encontrada: {campaign_id}")

        if campaign.status == CampaignStatus.CANCELLED.value:
            return {"status": "cancelled"}
        if campaign.status == CampaignStatus.PAUSED.value:
            return {"status": "paused"}

        run_result = await self.session.execute(
            select(GraphRun).where(GraphRun.campaign_id == campaign_id).order_by(GraphRun.created_at.desc())
        )
        graph_run = run_result.scalars().first()
        if graph_run is None:
            thread_id = thread_id or f"campaign-{campaign_id}"
            graph_run = GraphRun(
                id=uuid4().hex,
                campaign_id=campaign_id,
                thread_id=thread_id,
                status="running",
            )
            self.session.add(graph_run)
        else:
            thread_id = graph_run.thread_id
            graph_run.status = "running"
            graph_run.started_at = datetime.now(timezone.utc)

        campaign.status = CampaignStatus.RUNNING.value
        campaign.started_at = datetime.now(timezone.utc)
        await self.session.flush()

        cfg = campaign.config or {}
        initial_state: dict[str, Any] = {
            "campaign_id": campaign.id,
            "provider": campaign.provider,
            "niche": campaign.niche,
            "query": campaign.query or "",
            "city": campaign.city or "",
            "state": campaign.state or "",
            "max_results": campaign.max_results,
            "status": "running",
            "paused": False,
            "cancelled": False,
            "skip_email": bool(cfg.get("skip_email", False)),
            "logs": [],
            "companies": [],
            "contacts": [],
            "current_index": 0,
        }

        # Restaurar checkpoint se existir
        saved = await self.checkpoints.load(thread_id)
        if saved and campaign.status == CampaignStatus.PAUSED.value:
            initial_state.update(saved)
            initial_state["paused"] = False

        graph = get_campaign_graph()
        try:
            final_state = await graph.ainvoke(initial_state)
        except Exception as exc:
            campaign.status = CampaignStatus.FAILED.value
            campaign.error_message = str(exc)
            graph_run.status = "failed"
            graph_run.error_message = str(exc)
            graph_run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
            raise

        await self.checkpoints.save(thread_id, dict(final_state))

        # Persistir snapshot
        self.session.add(
            GraphStateRecord(
                id=uuid4().hex,
                graph_run_id=graph_run.id,
                node_name=final_state.get("status") or "finished",
                state_snapshot={
                    k: v
                    for k, v in final_state.items()
                    if k != "logs" or True
                },
            )
        )

        # Persistir empresas/contatos/items
        await self._persist_results(campaign, final_state)

        final_status = final_state.get("status") or "completed"
        if final_status in {"completed", "pipeline_updated", "waiting_response", "email_sent"}:
            campaign.status = CampaignStatus.COMPLETED.value
            graph_run.status = "completed"
        elif final_status == "paused":
            campaign.status = CampaignStatus.PAUSED.value
            graph_run.status = "paused"
        elif final_status == "cancelled":
            campaign.status = CampaignStatus.CANCELLED.value
            graph_run.status = "cancelled"
        elif final_status == "failed":
            campaign.status = CampaignStatus.FAILED.value
            graph_run.status = "failed"
        else:
            campaign.status = CampaignStatus.COMPLETED.value
            graph_run.status = "completed"

        campaign.finished_at = datetime.now(timezone.utc)
        graph_run.finished_at = datetime.now(timezone.utc)
        graph_run.current_node = "FinishCampaign"
        await self.session.flush()

        await self.session.commit()
        logger.info("campaign_finished", campaign_id=campaign_id, status=campaign.status)
        return dict(final_state)

    async def _persist_results(self, campaign: Campaign, state: dict[str, Any]) -> None:
        """Persiste somente leads com e-mail válido. Sem e-mail = descartado."""
        from app.providers.email_enrichment import has_valid_email

        contacts = state.get("contacts") or []
        # Nunca persiste companies cruas sem e-mail
        contacts = [c for c in contacts if has_valid_email(str(c.get("email") or ""))]
        if not contacts:
            logger.info("persist_skip_no_email", campaign_id=campaign.id)
            self.session.add(
                Activity(
                    id=uuid4().hex,
                    campaign_id=campaign.id,
                    activity_type="no_email_leads",
                    description="Nenhum lead com e-mail — todos descartados",
                )
            )
            await self.session.flush()
            return

        for raw in contacts:
            website = raw.get("website") or None
            website_host = None
            if website:
                from urllib.parse import urlparse

                host = urlparse(website if "://" in website else f"https://{website}").netloc
                website_host = (host or website)[:191]

            # Reutiliza empresa existente (evita unique constraint)
            existing_company = None
            if website_host or raw.get("company_name"):
                q = select(Company).where(
                    Company.name == (raw.get("company_name") or "Sem nome")[:191]
                )
                if website_host:
                    q = q.where(Company.website_host == website_host)
                if raw.get("city"):
                    q = q.where(Company.city == raw.get("city"))
                existing_company = (await self.session.execute(q.limit(1))).scalar_one_or_none()

            if existing_company:
                company = existing_company
                if raw.get("email") and not company.email:
                    company.email = raw.get("email")
            else:
                company = Company(
                    id=uuid4().hex,
                    name=(raw.get("company_name") or "Sem nome")[:191],
                    website=website,
                    website_host=website_host,
                    phone=raw.get("phone") or None,
                    email=raw.get("email") or None,
                    city=(raw.get("city") or None),
                    state=raw.get("state") or None,
                    segment=raw.get("segment") or campaign.niche,
                    source=raw.get("source") or None,
                    crm_id=state.get("crm_company_id"),
                    extra=raw.get("extra"),
                )
                self.session.add(company)
                await self.session.flush()

            contact = Contact(
                id=uuid4().hex,
                company_id=company.id,
                name=raw.get("contact_name") or raw.get("company_name") or "Contato",
                email=raw.get("email"),
                phone=raw.get("phone") or None,
                source=raw.get("source") or None,
                crm_id=state.get("crm_contact_id"),
                extra=raw.get("extra"),
            )
            self.session.add(contact)
            await self.session.flush()

            item = CampaignItem(
                id=uuid4().hex,
                campaign_id=campaign.id,
                company_id=company.id,
                contact_id=contact.id,
                status=ItemStatus.EMAIL_SENT.value
                if state.get("email_status") in {"sent", "dry_run"}
                else ItemStatus.EMAIL_FOUND.value,
                source=raw.get("source"),
                raw_data=raw,
                qualification_score=state.get("qualification_score"),
                qualification_notes=state.get("qualification_notes"),
                crm_company_id=state.get("crm_company_id"),
                crm_contact_id=state.get("crm_contact_id"),
                crm_lead_id=state.get("crm_lead_id"),
                template_name=state.get("template_name"),
            )
            self.session.add(item)

        self.session.add(
            Activity(
                id=uuid4().hex,
                campaign_id=campaign.id,
                activity_type="campaign_finished",
                description=f"Status final: {state.get('status')}",
                payload={"logs_count": len(state.get("logs") or [])},
            )
        )
        await self.session.flush()

    async def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        result = await self.session.execute(
            select(Campaign)
            .where(Campaign.id == campaign_id)
            .options(selectinload(Campaign.items), selectinload(Campaign.graph_runs))
        )
        return result.scalar_one_or_none()

    async def get_status(self, campaign_id: str) -> dict[str, Any]:
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")
        items_total = len(campaign.items)
        items_by_status: dict[str, int] = {}
        for item in campaign.items:
            items_by_status[item.status] = items_by_status.get(item.status, 0) + 1
        run = campaign.graph_runs[0] if campaign.graph_runs else None
        return {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "niche": campaign.niche,
            "provider": campaign.provider,
            "items_total": items_total,
            "items_by_status": items_by_status,
            "started_at": campaign.started_at,
            "finished_at": campaign.finished_at,
            "error_message": campaign.error_message,
            "graph_run": {
                "thread_id": run.thread_id if run else None,
                "status": run.status if run else None,
                "current_node": run.current_node if run else None,
            },
        }

    async def pause(self, campaign_id: str) -> Campaign:
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")
        campaign.status = CampaignStatus.PAUSED.value
        await self.session.commit()
        return await self.get_campaign(campaign_id)  # type: ignore[return-value]

    async def resume(self, campaign_id: str) -> Campaign:
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")
        if campaign.status != CampaignStatus.PAUSED.value:
            raise ValueError("Campanha não está pausada")
        campaign.status = CampaignStatus.PENDING.value
        run = campaign.graph_runs[0] if campaign.graph_runs else None
        thread_id = run.thread_id if run else f"campaign-{campaign_id}"
        await self.session.commit()
        asyncio.create_task(self._run_campaign_safe(campaign_id, thread_id))
        return await self.get_campaign(campaign_id)  # type: ignore[return-value]

    async def cancel(self, campaign_id: str) -> Campaign:
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("Campanha não encontrada")
        campaign.status = CampaignStatus.CANCELLED.value
        campaign.finished_at = datetime.now(timezone.utc)
        await self.session.commit()
        return await self.get_campaign(campaign_id)  # type: ignore[return-value]

    async def list_companies(
        self, *, offset: int = 0, limit: int = 50, segment: str | None = None
    ) -> tuple[list[Company], int]:
        q = select(Company)
        count_q = select(func.count()).select_from(Company)
        if segment:
            q = q.where(Company.segment == segment)
            count_q = count_q.where(Company.segment == segment)
        total = (await self.session.execute(count_q)).scalar() or 0
        result = await self.session.execute(q.offset(offset).limit(limit).order_by(Company.created_at.desc()))
        return list(result.scalars().all()), int(total)

    async def list_contacts(
        self, *, offset: int = 0, limit: int = 50
    ) -> tuple[list[Contact], int]:
        total = (
            await self.session.execute(select(func.count()).select_from(Contact))
        ).scalar() or 0
        result = await self.session.execute(
            select(Contact).offset(offset).limit(limit).order_by(Contact.created_at.desc())
        )
        return list(result.scalars().all()), int(total)

    async def dashboard(self) -> dict[str, Any]:
        campaigns_total = (
            await self.session.execute(select(func.count()).select_from(Campaign))
        ).scalar() or 0
        companies_total = (
            await self.session.execute(select(func.count()).select_from(Company))
        ).scalar() or 0
        contacts_total = (
            await self.session.execute(select(func.count()).select_from(Contact))
        ).scalar() or 0
        running = (
            await self.session.execute(
                select(func.count())
                .select_from(Campaign)
                .where(Campaign.status == CampaignStatus.RUNNING.value)
            )
        ).scalar() or 0
        completed = (
            await self.session.execute(
                select(func.count())
                .select_from(Campaign)
                .where(Campaign.status == CampaignStatus.COMPLETED.value)
            )
        ).scalar() or 0

        by_niche = await self.session.execute(
            select(Campaign.niche, func.count())
            .group_by(Campaign.niche)
        )
        niche_counts = {row[0]: row[1] for row in by_niche.all()}

        return {
            "campaigns_total": int(campaigns_total),
            "campaigns_running": int(running),
            "campaigns_completed": int(completed),
            "companies_total": int(companies_total),
            "contacts_total": int(contacts_total),
            "campaigns_by_niche": niche_counts,
            "providers": get_provider_registry().list_providers(),
        }

    async def seed_providers(self) -> int:
        registry = get_provider_registry()
        count = 0
        for p in registry.list_providers():
            existing = await self.session.execute(
                select(Provider).where(Provider.code == p["code"])
            )
            if existing.scalar_one_or_none():
                continue
            self.session.add(
                Provider(
                    id=uuid4().hex,
                    code=p["code"],
                    name=p["name"],
                    niche=p["niche"],
                    template_file=p["template_file"],
                    strategies=p["strategies"],
                    enabled=True,
                )
            )
            count += 1
        await self.session.flush()
        return count
