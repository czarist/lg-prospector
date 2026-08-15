"""Mailman — disparo de e-mail desacoplado da prospecção.

Hunts só cadastram leads. Este serviço avalia quem ainda não recebeu
e-mail nos últimos N dias e envia em lotes de 4 — prefere 2 nicho + 2 generalista e completa
com a faixa que ainda tiver gente. Nunca para por falta de um lado.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.models import (
    Campaign,
    CampaignItem,
    Company,
    Contact,
    EmailRecord,
    ItemStatus,
)
from app.infrastructure.email.generalist_copy import TEMPLATE_FILE
from app.infrastructure.email.smtp import (
    is_smtp_provider_block,
    smtp_quota_status,
)
from app.providers.domain_email import extract_registrable_domain
from app.providers.generalista import ORIGIN
from app.providers.geo_email import classify_contact_email, is_plausible_lead
from app.providers.public_org import is_public_email, is_public_organ
from app.services.generalist_service import NICHE as GENERALIST_NICHE
from app.services.generalist_service import GeneralistService
from app.services.stage_service import StageService

logger = get_logger(__name__)

Lane = Literal["niche", "generalista"]

_EXCLUDED_SEGMENTS = {"politico", "partido"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _email_domain(addr: str) -> str:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1]


def _host_of(company: Company | None) -> str:
    if not company:
        return ""
    if company.website_host:
        return company.website_host.strip().lower()
    if company.website:
        host = urlparse(
            company.website if "://" in company.website else f"https://{company.website}"
        ).netloc
        return (host or "").lower()
    return extract_registrable_domain(company.website or "") or ""


@dataclass(slots=True)
class MailTarget:
    lane: Lane
    email: str
    contact: Contact
    company: Company | None
    item: CampaignItem | None
    campaign: Campaign | None
    niche: str
    city: str
    domain: str


class MailmanService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.stage_svc = StageService(session)
        self.gen_svc = GeneralistService(session)

    async def run_batch(
        self,
        *,
        dry_run: bool = False,
        batch_size: int | None = None,
        cooldown_days: int | None = None,
        wait_days: int | None = None,
        only: Lane | None = None,
        intra_pause: bool = True,
    ) -> dict[str, Any]:
        settings = get_settings()
        size = max(1, int(batch_size if batch_size is not None else settings.mailman_batch_size))
        cooldown = int(
            cooldown_days if cooldown_days is not None else settings.email_cooldown_days
        )
        wait = int(wait_days if wait_days is not None else settings.email_cooldown_days)

        quota_ok, quota_why = await smtp_quota_status(self.session)
        if not quota_ok:
            logger.warning("mailman_quota_pause", reason=quota_why)
            return {
                "sent": 0,
                "failed": 0,
                "skipped": 0,
                "candidates": 0,
                "picked": 0,
                "quota_paused": True,
                "quota_reason": quota_why,
                "provider_blocked": False,
                "dry_run": dry_run,
                "results": [],
            }

        pool = await self.collect_targets(
            cooldown_days=cooldown,
            wait_days=wait,
            only=only,
            overfetch=max(80, size * 40),
            persist_rejects=not dry_run,
        )
        prefer_mix = only is None and size >= 2
        picked = self.pick_batch(pool, size, prefer_mix=prefer_mix)
        niche_n = sum(1 for t in pool if t.lane == "niche")
        gen_n = sum(1 for t in pool if t.lane == "generalista")
        picked_niche = sum(1 for t in picked if t.lane == "niche")
        picked_gen = sum(1 for t in picked if t.lane == "generalista")
        results: list[dict[str, Any]] = []
        sent = failed = skipped = 0
        provider_blocked = False

        intra_min = float(settings.mailman_intra_batch_min_seconds)
        intra_max = float(settings.mailman_intra_batch_max_seconds)
        used = list(picked)
        unused = [t for t in pool if t.email not in {u.email for u in used}]

        for i, target in enumerate(picked):
            if provider_blocked:
                break
            quota_ok, quota_why = await smtp_quota_status(self.session)
            if not quota_ok:
                skipped += 1
                results.append(
                    {
                        "lane": target.lane,
                        "to": target.email,
                        "outcome": "quota",
                        "reason": quota_why,
                    }
                )
                break

            outcome = await self.send_target(
                target,
                dry_run=dry_run,
                cooldown_days=cooldown,
                wait_days=wait,
            )
            # se o escolhido pular, tenta outro da mesma faixa; se acabar, completa com a outra
            tries = 0
            while (
                outcome.get("outcome") == "skip"
                and tries < 8
            ):
                same = [t for t in unused if t.lane == target.lane]
                alt = self._next_diverse(same, used) or self._next_diverse(unused, used)
                if alt is None:
                    break
                unused = [t for t in unused if t.email != alt.email]
                used.append(alt)
                logger.info(
                    "mailman_retry_lane",
                    lane=target.lane,
                    skipped=target.email,
                    reason=outcome.get("reason"),
                    retry=alt.email,
                    retry_lane=alt.lane,
                )
                outcome = await self.send_target(
                    alt,
                    dry_run=dry_run,
                    cooldown_days=cooldown,
                    wait_days=wait,
                )
                target = alt
                tries += 1
            results.append(outcome)
            status = outcome.get("outcome")
            if status in {"sent", "dry_run"}:
                sent += 1
            elif status == "blocked":
                provider_blocked = True
            elif status in {"failed", "blocked"}:
                failed += 1
            else:
                skipped += 1

            if (
                intra_pause
                and i + 1 < len(picked)
                and not provider_blocked
                and status in {"sent", "dry_run", "failed"}
            ):
                pause = random.uniform(min(intra_min, intra_max), max(intra_min, intra_max))
                logger.info("mailman_intra_pause", seconds=round(pause, 1))
                await asyncio.sleep(pause)

        summary = {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "candidates": len(pool),
            "niche": niche_n,
            "generalista": gen_n,
            "picked": len(picked),
            "picked_niche": picked_niche,
            "picked_gen": picked_gen,
            "quota_paused": False,
            "provider_blocked": provider_blocked,
            "dry_run": dry_run,
            "cooldown_days": cooldown,
            "results": results,
            "forecast": await self.forecast(cooldown_days=cooldown, wait_days=wait),
        }
        logger.info(
            "mailman_batch_done",
            sent=sent,
            failed=failed,
            skipped=skipped,
            candidates=len(pool),
            niche=niche_n,
            generalista=gen_n,
            picked=len(picked),
            picked_niche=picked_niche,
            picked_gen=picked_gen,
            blocked=provider_blocked,
        )
        return summary

    async def preview(
        self,
        *,
        cooldown_days: int | None = None,
        wait_days: int | None = None,
        only: Lane | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        settings = get_settings()
        cooldown = int(
            cooldown_days if cooldown_days is not None else settings.email_cooldown_days
        )
        wait = int(wait_days if wait_days is not None else settings.email_cooldown_days)
        pool = await self.collect_targets(
            cooldown_days=cooldown,
            wait_days=wait,
            only=only,
            overfetch=max(limit * 3, 80),
        )
        niche_n = sum(1 for t in pool if t.lane == "niche")
        gen_n = sum(1 for t in pool if t.lane == "generalista")
        sample = [
            {
                "lane": t.lane,
                "niche": t.niche,
                "to": t.email,
                "company": (t.company.name if t.company else t.contact.name),
                "city": t.city,
            }
            for t in pool[:limit]
        ]
        forecast = await self.forecast(cooldown_days=cooldown, wait_days=wait)
        return {
            "candidates": len(pool),
            "niche": niche_n,
            "generalista": gen_n,
            "cooldown_days": cooldown,
            "wait_days": wait,
            "sample": sample,
            "forecast": forecast,
        }

    async def forecast(
        self,
        *,
        cooldown_days: int | None = None,
        wait_days: int | None = None,
    ) -> dict[str, Any]:
        """Previsão da fila: enviados / total, prontos agora e espera de 4 dias.

        Nicho e geral são modalidades independentes no mesmo lead:
        - lead de nicho entra nas duas filas (template do nicho + o geral)
        - lead só-geral não entra na fila de nicho
        - político/partido ficam de fora do template geral (e-mail de negócio)
        Total cresce com cadastro novo; "prontos" sobe quando vence a espera
        de `wait_days` (só descoberta generalista) e quando sai do cooldown.
        """
        settings = get_settings()
        cooldown = int(
            cooldown_days if cooldown_days is not None else settings.email_cooldown_days
        )
        wait = int(wait_days if wait_days is not None else settings.email_cooldown_days)
        now = _utcnow()
        wait_cutoff = now - timedelta(days=max(0, wait))
        cool_since = now - timedelta(days=max(0, cooldown))
        recent = self._email_exists(statuses=("sent", "dry_run"), since=cool_since)
        bounced = self._email_exists(statuses=("bounced",))
        sent_gen = self._email_exists(
            templates=(TEMPLATE_FILE,),
            statuses=("sent", "dry_run"),
        )
        excluded = list(_EXCLUDED_SEGMENTS)
        niche_stages = (
            ItemStageStatus.CRM_SYNCED.value,
            ItemStageStatus.SENT.value,
            ItemStageStatus.QUEUED.value,
        )

        niche_total = await self._count(
            select(func.count(CampaignItem.id))
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .join(Contact, CampaignItem.contact_id == Contact.id)
            .where(
                func.lower(Campaign.niche) != GENERALIST_NICHE,
                Contact.email.is_not(None),
                Contact.email != "",
                CampaignItem.stage.in_(niche_stages),
            )
        )
        niche_sent = await self._count(
            select(func.count(CampaignItem.id))
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .join(Contact, CampaignItem.contact_id == Contact.id)
            .where(
                func.lower(Campaign.niche) != GENERALIST_NICHE,
                Contact.email.is_not(None),
                Contact.email != "",
                CampaignItem.stage.in_(niche_stages),
                or_(
                    CampaignItem.stage == ItemStageStatus.SENT.value,
                    CampaignItem.email_sent_at.is_not(None),
                ),
            )
        )
        niche_ready = await self._count(
            select(func.count(CampaignItem.id))
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .join(Contact, CampaignItem.contact_id == Contact.id)
            .where(
                func.lower(Campaign.niche) != GENERALIST_NICHE,
                Contact.email.is_not(None),
                Contact.email != "",
                CampaignItem.stage == ItemStageStatus.CRM_SYNCED.value,
                ~recent,
                ~bounced,
            )
        )
        niche_left = max(0, niche_total - niche_sent)
        niche_cooldown = max(0, niche_left - niche_ready)

        has_email = (
            Contact.email.is_not(None),
            Contact.email != "",
            or_(Company.segment.is_(None), Company.segment.notin_(excluded)),
        )
        in_geral = or_(
            and_(Contact.crm_id.is_not(None), Contact.crm_id != ""),
            Company.source == ORIGIN,
        )
        gen_total = await self._count(
            select(func.count(Contact.id))
            .join(Company, Contact.company_id == Company.id)
            .where(*has_email, in_geral)
        )
        gen_sent = await self._count(
            select(func.count(Contact.id))
            .join(Company, Contact.company_id == Company.id)
            .where(*has_email, in_geral, sent_gen)
        )
        gen_waiting = await self._count(
            select(func.count(Contact.id))
            .join(Company, Contact.company_id == Company.id)
            .where(
                *has_email,
                Company.source == ORIGIN,
                Company.created_at > wait_cutoff,
                ~sent_gen,
            )
        )
        gen_ready_exist = await self._count(
            select(func.count(Contact.id))
            .join(Company, Contact.company_id == Company.id)
            .where(
                *has_email,
                Contact.crm_id.is_not(None),
                Contact.crm_id != "",
                or_(
                    Company.source.is_(None),
                    Company.source != ORIGIN,
                    func.coalesce(Company.source, "") == "",
                ),
                ~sent_gen,
                ~recent,
                ~bounced,
            )
        )
        gen_ready_mature = await self._count(
            select(func.count(Contact.id))
            .join(Company, Contact.company_id == Company.id)
            .where(
                *has_email,
                Company.source == ORIGIN,
                Company.created_at <= wait_cutoff,
                ~sent_gen,
                ~recent,
                ~bounced,
            )
        )
        gen_ready = gen_ready_exist + gen_ready_mature
        gen_left = max(0, gen_total - gen_sent)
        gen_cooldown = max(0, gen_left - gen_ready - gen_waiting)

        return {
            "cooldown_days": cooldown,
            "wait_days": wait,
            "niche": {
                "sent": niche_sent,
                "ready": niche_ready,
                "waiting": 0,
                "cooldown": niche_cooldown,
                "total": niche_total,
            },
            "generalista": {
                "sent": gen_sent,
                "ready": gen_ready,
                "waiting": gen_waiting,
                "cooldown": gen_cooldown,
                "total": gen_total,
            },
        }

    async def _count(self, stmt) -> int:
        return int((await self.session.execute(stmt)).scalar() or 0)

    def _email_exists(
        self,
        *,
        statuses: tuple[str, ...],
        templates: tuple[str, ...] | None = None,
        since: datetime | None = None,
    ):
        clauses: list[Any] = [EmailRecord.status.in_(list(statuses))]
        if templates:
            clauses.append(EmailRecord.template_name.in_(list(templates)))
        if since is not None:
            clauses.append(EmailRecord.sent_at.is_not(None))
            clauses.append(EmailRecord.sent_at >= since)
        clauses.append(
            or_(
                EmailRecord.contact_id == Contact.id,
                func.lower(EmailRecord.to_address) == func.lower(Contact.email),
            )
        )
        return exists(select(EmailRecord.id).where(*clauses))

    async def collect_targets(
        self,
        *,
        cooldown_days: int,
        wait_days: int,
        only: Lane | None,
        overfetch: int,
        persist_rejects: bool = False,
    ) -> list[MailTarget]:
        half = max(8, overfetch // 2)
        targets: list[MailTarget] = []
        if only != "generalista":
            targets.extend(
                await self._niche_targets(
                    cooldown_days=cooldown_days,
                    limit=half,
                    persist_rejects=persist_rejects,
                )
            )
        if only != "niche":
            targets.extend(
                await self._generalist_targets(
                    cooldown_days=cooldown_days,
                    wait_days=wait_days,
                    limit=max(40, half),
                    persist_rejects=persist_rejects,
                )
            )
        # cooldown global (qualquer template). Mesmo e-mail pode aparecer
        # nas duas faixas; pick_batch não dispara os dois no mesmo lote.
        kept: list[MailTarget] = []
        seen: set[tuple[str, str]] = set()
        since = _utcnow() - timedelta(days=max(0, cooldown_days))
        for t in targets:
            key = (t.lane, t.email)
            if key in seen:
                continue
            if await self.stage_svc._email_was_bounced(
                t.email, contact_id=t.contact.id
            ):
                continue
            if await self.stage_svc._email_in_cooldown(
                t.email,
                contact_id=t.contact.id,
                since=since,
            ):
                continue
            seen.add(key)
            kept.append(t)
        random.shuffle(kept)
        return kept

    def pick_batch(
        self,
        pool: list[MailTarget],
        size: int,
        *,
        prefer_mix: bool = True,
        require_pair: bool | None = None,
    ) -> list[MailTarget]:
        """Lote de `size` envios. Prefere metade/metade (2+2) e completa
        com a faixa que ainda tiver gente — 1+3, 0+4, 3+1, 4+0.

        Só devolve vazio se o pool inteiro estiver seco. `prefer_mix=False`
        no `--only` (uma faixa).
        """
        if require_pair is not None:
            prefer_mix = require_pair
        if not pool or size <= 0:
            return []
        niche = [t for t in pool if t.lane == "niche"]
        gen = [t for t in pool if t.lane == "generalista"]
        random.shuffle(niche)
        random.shuffle(gen)

        if not prefer_mix:
            lane = niche or gen
            chosen: list[MailTarget] = []
            while len(chosen) < size:
                nxt = self._next_diverse(lane, chosen)
                if nxt is None:
                    break
                chosen.append(nxt)
            return chosen

        per_lane = max(1, size // 2)
        chosen: list[MailTarget] = []
        n_got = g_got = 0
        while n_got < per_lane or g_got < per_lane:
            progressed = False
            if n_got < per_lane:
                nxt = self._next_diverse(niche, chosen)
                if nxt:
                    chosen.append(nxt)
                    n_got += 1
                    progressed = True
            if g_got < per_lane:
                nxt = self._next_diverse(gen, chosen)
                if nxt:
                    chosen.append(nxt)
                    g_got += 1
                    progressed = True
            if not progressed:
                break

        # improviso: o que faltou de uma faixa sai da outra
        while len(chosen) < size:
            nxt = self._next_diverse(niche, chosen) or self._next_diverse(gen, chosen)
            if nxt is None:
                break
            chosen.append(nxt)
        random.shuffle(chosen)
        return chosen

    def _next_diverse(
        self, candidates: list[MailTarget], already: list[MailTarget]
    ) -> MailTarget | None:
        used_email = {t.email for t in already}
        used_domain = {t.domain for t in already if t.domain}
        used_host = {_host_of(t.company) for t in already}
        used_host.discard("")
        used_niche = {t.niche for t in already}
        used_city = {t.city for t in already if t.city}

        def take(pred) -> MailTarget | None:
            for t in candidates:
                if t.email in used_email:
                    continue
                if t.domain and t.domain in used_domain:
                    continue
                host = _host_of(t.company)
                if host and host in used_host:
                    continue
                if pred(t):
                    return t
            return None

        # passa 1: nicho e cidade diferentes
        hit = take(lambda t: t.niche not in used_niche and t.city not in used_city)
        if hit:
            return hit
        # passa 2: só nicho diferente
        hit = take(lambda t: t.niche not in used_niche)
        if hit:
            return hit
        # passa 3: qualquer domínio/e-mail distinto
        return take(lambda _t: True)

    async def send_target(
        self,
        target: MailTarget,
        *,
        dry_run: bool,
        cooldown_days: int,
        wait_days: int | None = None,
    ) -> dict[str, Any]:
        base = {
            "lane": target.lane,
            "niche": target.niche,
            "to": target.email,
            "company": (target.company.name if target.company else target.contact.name),
            "city": target.city,
        }
        if target.lane == "niche":
            if not target.item:
                return {**base, "outcome": "skip", "reason": "sem_item"}
            result = await self.stage_svc.send_one_item(
                target.item,
                campaign=target.campaign,
                dry_run=dry_run,
                cooldown_days=cooldown_days,
            )
            await self.session.flush()
            return {**base, **result}

        mature_days = cooldown_days if wait_days is None else wait_days
        ok, why = await self.gen_svc._can_send(
            target.contact,
            target.company,
            require_wait=target.item is not None,
            wait_cutoff=_utcnow() - timedelta(days=max(0, mature_days)),
            only_origin=ORIGIN if target.item is not None else None,
        )
        if not ok:
            return {**base, "outcome": "skip", "reason": why}

        report = self.gen_svc._report_for(target.company)
        status = await self.gen_svc._send_one(
            target.contact,
            target.company,
            report,
            dry_run=dry_run,
            item=target.item,
        )
        if target.item and status in {"sent", "dry_run"}:
            target.item.stage = ItemStageStatus.SENT.value
            target.item.status = ItemStatus.EMAIL_SENT.value
            target.item.template_name = target.item.template_name or TEMPLATE_FILE
            target.item.email_sent_at = _utcnow()
        elif target.item and status == "blocked":
            target.item.error_message = "smtp_provider_block"
        elif target.item and status not in {"sent", "dry_run"}:
            target.item.error_message = status or "send_failed"
        await self.session.flush()
        if status == "blocked" or is_smtp_provider_block(status):
            return {**base, "outcome": "blocked", "reason": "smtp_provider_block"}
        if status in {"sent", "dry_run"}:
            return {**base, "outcome": status, "reason": "ok"}
        return {**base, "outcome": "failed", "reason": status or "send_failed"}

    # ------------------------------------------------------------------
    # coleta
    # ------------------------------------------------------------------
    async def _niche_targets(
        self,
        *,
        cooldown_days: int,
        limit: int,
        persist_rejects: bool = False,
    ) -> list[MailTarget]:
        """Page past junk/cooldown-clogged rows until `limit` usable niche targets."""
        recent = self._recent_email_exists(cooldown_days)
        out: list[MailTarget] = []
        rejects: list[CampaignItem] = []
        offset = 0
        page = 80
        scanned = 0
        max_scan = 600
        while len(out) < limit and scanned < max_scan:
            q = (
                select(CampaignItem)
                .join(Campaign, CampaignItem.campaign_id == Campaign.id)
                .join(Contact, CampaignItem.contact_id == Contact.id)
                .outerjoin(Company, CampaignItem.company_id == Company.id)
                .where(
                    CampaignItem.stage == ItemStageStatus.CRM_SYNCED.value,
                    func.lower(Campaign.niche) != GENERALIST_NICHE,
                    Contact.email.is_not(None),
                    Contact.email != "",
                    ~recent,
                )
                .options(
                    selectinload(CampaignItem.company),
                    selectinload(CampaignItem.contact),
                    selectinload(CampaignItem.campaign),
                )
                .order_by(CampaignItem.created_at.asc())
                .offset(offset)
                .limit(page)
            )
            items = list((await self.session.execute(q)).scalars().unique().all())
            if not items:
                break
            offset += len(items)
            scanned += len(items)
            for item in items:
                target = self._niche_from_item(item)
                if not target:
                    continue
                why = self._reject_reason(target)
                if why:
                    if persist_rejects and why.startswith(("orgao_publico", "email_implausivel")):
                        rejects.append(item)
                        item.error_message = why
                    continue
                out.append(target)
                if len(out) >= limit:
                    break
        if persist_rejects and rejects:
            for item in rejects:
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
            await self.session.flush()
            logger.info("mailman_rejected_niche", n=len(rejects))
        return out

    async def _generalist_targets(
        self,
        *,
        cooldown_days: int,
        wait_days: int,
        limit: int,
        persist_rejects: bool = False,
    ) -> list[MailTarget]:
        half = max(4, limit // 2)
        out: list[MailTarget] = []
        contacts = await self.gen_svc._eligible_existing_contacts(limit=max(80, half * 6))
        for ct in contacts:
            target = self._from_contact(ct, lane="generalista", item=None)
            if target and self._passes_common_filters(target):
                out.append(target)

        cutoff = _utcnow() - timedelta(days=max(0, wait_days))
        items = await self.gen_svc._mature_generalist_items(cutoff, limit=max(40, half * 3))
        rejects: list[CampaignItem] = []
        for item in items:
            if not item.contact:
                continue
            target = self._from_contact(
                item.contact,
                lane="generalista",
                item=item,
                company=item.company,
            )
            if not target:
                continue
            why = self._reject_reason(target)
            if why:
                if persist_rejects and why.startswith(("orgao_publico", "email_implausivel")):
                    rejects.append(item)
                    item.error_message = why
                continue
            out.append(target)
        if persist_rejects and rejects:
            for item in rejects:
                item.stage = ItemStageStatus.FAILED.value
                item.status = ItemStatus.FAILED.value
            await self.session.flush()
            logger.info("mailman_rejected_generalista", n=len(rejects))
        return out

    def _recent_email_exists(self, cooldown_days: int):
        cutoff = _utcnow() - timedelta(days=max(0, int(cooldown_days)))
        return exists(
            select(EmailRecord.id).where(
                EmailRecord.status.in_(["sent", "dry_run"]),
                EmailRecord.sent_at.is_not(None),
                EmailRecord.sent_at >= cutoff,
                or_(
                    EmailRecord.contact_id == Contact.id,
                    func.lower(EmailRecord.to_address) == func.lower(Contact.email),
                ),
            )
        )

    def _niche_from_item(self, item: CampaignItem) -> MailTarget | None:
        contact = item.contact
        if not contact or not (contact.email or "").strip():
            return None
        campaign = item.campaign
        company = item.company
        email = contact.email.strip().lower()
        niche = (campaign.niche if campaign else "") or (company.segment if company else "") or ""
        return MailTarget(
            lane="niche",
            email=email,
            contact=contact,
            company=company,
            item=item,
            campaign=campaign,
            niche=niche.lower(),
            city=((company.city if company else "") or (campaign.city if campaign else "") or ""),
            domain=_email_domain(email),
        )

    def _from_contact(
        self,
        contact: Contact,
        *,
        lane: Lane,
        item: CampaignItem | None,
        company: Company | None = None,
    ) -> MailTarget | None:
        email = (contact.email or "").strip().lower()
        if not email or "@" not in email:
            return None
        company = company if company is not None else contact.company
        niche = (
            (item.campaign.niche if item and item.campaign else "")
            or ((company.segment if company else "") or GENERALIST_NICHE)
        )
        return MailTarget(
            lane=lane,
            email=email,
            contact=contact,
            company=company,
            item=item,
            campaign=item.campaign if item else None,
            niche=(niche or GENERALIST_NICHE).lower(),
            city=(company.city if company else "") or "",
            domain=_email_domain(email),
        )

    def _reject_reason(self, target: MailTarget) -> str | None:
        extra_c = target.contact.extra or {}
        extra_co = (target.company.extra if target.company else None) or {}
        if extra_c.get("opt_out") or extra_co.get("opt_out"):
            return "opt_out"
        if extra_c.get("negociacao_ativa") or extra_co.get("negociacao_ativa"):
            return "negociacao_ativa"
        seg = ((target.company.segment if target.company else "") or target.niche or "").lower()
        if target.lane == "generalista" and seg in _EXCLUDED_SEGMENTS:
            return "segmento_excluido"
        judge_seg = GENERALIST_NICHE if target.lane == "generalista" else (seg or target.niche)
        allow_gov = target.lane == "generalista"
        if is_public_email(target.email, allow_gov_br=allow_gov) or is_public_organ(
            name=(target.company.name if target.company else "") or target.contact.name or "",
            website=(target.company.website if target.company else "") or "",
            email=target.email,
            segment=judge_seg,
            allow_gov_br=allow_gov,
        ):
            return "orgao_publico"
        if target.lane == "generalista" and target.company and not is_plausible_lead(
            name=target.company.name or "",
            website=target.company.website or "",
            email=target.email,
            snippet=str(extra_co.get("snippet") or ""),
            segment=GENERALIST_NICHE,
        ):
            return "fora_do_nicho_ou_nacionalidade"
        ok_geo, reason = classify_contact_email(
            target.email,
            name=target.contact.name or (target.company.name if target.company else "") or "",
            city=target.city,
            party=str(extra_co.get("tse_partido") or ""),
            website=(target.company.website if target.company else "") or "",
            segment=judge_seg,
        )
        if not ok_geo:
            return f"email_implausivel:{reason}"
        return None

    def _passes_common_filters(self, target: MailTarget) -> bool:
        return self._reject_reason(target) is None
