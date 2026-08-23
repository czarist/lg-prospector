"""Consome a fila de revisão: Qwen local decide KEEP/DROP e só então grava."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.infrastructure.database.models import Campaign, CampaignStatus
from app.infrastructure.llm.client import score_company_candidate
from app.providers.geo_email import is_junk_lead_name
from app.services.review_queue import ReviewQueue, candidate_dedup_key
from app.services.stage_service import StageService

logger = get_logger(__name__)

GENERALIST_NICHE = "generalista"
_HARD_DROP_MARKERS = (
    "estrangeir",
    "foreign",
    "wikipedia",
    "listicle",
    "vaga de emprego",
    "não é empresa",
    "nao e empresa",
)


def _is_lock_timeout(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    text = f"{exc} {orig or ''}"
    return "1205" in text or "Lock wait timeout" in text


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.stages = StageService(session)
        self.queue = ReviewQueue()

    async def enqueue_campaign_hits(
        self,
        campaign: Campaign,
        found: list[ProviderResult],
    ) -> int:
        queued = 0
        niche = (campaign.niche or "").lower()
        for pr in found:
            origin = pr.segment or campaign.niche
            pr.segment = origin
            niche_ok = pr.is_valid_company()
            geral_ok = False
            if not niche_ok and niche != GENERALIST_NICHE:
                pr.segment = GENERALIST_NICHE
                geral_ok = pr.is_valid_company()
            if not niche_ok and not geral_ok:
                continue
            pr.segment = origin
            payload = {
                "campaign_id": campaign.id,
                "niche": campaign.niche,
                "city": campaign.city or pr.city or "",
                "state": campaign.state or pr.state or "",
                "dedup_key": candidate_dedup_key(
                    niche=niche,
                    name=pr.company_name or "",
                    website=pr.website or "",
                    email=pr.email or "",
                ),
                "candidate": pr.model_dump(),
                "attempts": 0,
                "heuristic_niche_ok": niche_ok,
            }
            if await self.queue.enqueue(payload):
                queued += 1
        if queued:
            logger.info(
                "review_enqueued",
                campaign_id=campaign.id,
                niche=campaign.niche,
                queued=queued,
                pool=len(found),
            )
        return queued

    async def process_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()
        campaign_id = str(payload.get("campaign_id") or "")
        raw = payload.get("candidate") or {}
        if not campaign_id or not isinstance(raw, dict):
            return {"outcome": "drop", "reason": "payload_invalido"}

        try:
            pr = ProviderResult(**raw)
        except Exception as exc:
            return {"outcome": "drop", "reason": f"candidato_invalido:{type(exc).__name__}"}

        origin_niche = str(payload.get("niche") or pr.segment or "").strip().lower()
        pr.segment = pr.segment or origin_niche
        name = pr.company_name or ""
        if is_junk_lead_name(name):
            await self.queue.forget(str(payload.get("dedup_key") or ""))
            return {"outcome": "drop", "reason": "nome_lixo", "name": name}
        already_generalista = origin_niche == GENERALIST_NICHE
        heuristic_niche_ok = payload.get("heuristic_niche_ok")
        if heuristic_niche_ok is None:
            heuristic_niche_ok = pr.is_valid_company()

        if not heuristic_niche_ok:
            if already_generalista or not self._fits_generalista(pr):
                await self.queue.forget(str(payload.get("dedup_key") or ""))
                return {
                    "outcome": "drop",
                    "reason": "fora_do_nicho_ou_nacionalidade",
                    "name": name,
                }

        campaign = await self.stages.get_campaign(campaign_id)
        if not campaign:
            return {"outcome": "drop", "reason": "campanha_ausente", "name": name}

        city = campaign.city or pr.city or str(payload.get("city") or "")
        state = campaign.state or pr.state or str(payload.get("state") or "")
        pr.city = pr.city or city
        pr.state = pr.state or state
        attempts = int(payload.get("attempts") or 0)
        force = bool(settings.hunt_use_llm)
        min_score = int(settings.discover_min_llm_score)

        try_geral = (not heuristic_niche_ok) and not already_generalista
        score: dict[str, Any] = {}
        if not try_geral:
            score = await self._score(pr, niche=campaign.niche, city=city, force=force)
            if score.get("error") and attempts < 2:
                payload["attempts"] = attempts + 1
                await self.queue.requeue(payload)
                return {
                    "outcome": "retry",
                    "reason": str(score.get("reason") or "llm_error"),
                    "name": name,
                }
            if self._is_keep(score, min_score):
                return await self._persist(
                    campaign,
                    pr,
                    score,
                    origin_niche=origin_niche,
                )
            if already_generalista or self._hard_drop(str(score.get("reason") or "")):
                return await self._drop(payload, pr, score, origin_niche)
            if not self._fits_generalista(pr):
                return await self._drop(payload, pr, score, origin_niche)
            try_geral = True

        score_g = await self._score(pr, niche=GENERALIST_NICHE, city=city, force=force)
        if score_g.get("error") and attempts < 2:
            payload["attempts"] = attempts + 1
            payload["heuristic_niche_ok"] = False
            await self.queue.requeue(payload)
            return {
                "outcome": "retry",
                "reason": str(score_g.get("reason") or "llm_error"),
                "name": name,
            }
        if not self._is_keep(score_g, min_score):
            merged = dict(score_g)
            if score.get("reason"):
                merged["reason"] = (
                    f"{score.get('reason')}; geral:{score_g.get('reason') or 'drop'}"
                )[:120]
            return await self._drop(payload, pr, merged, origin_niche)

        home = await self._generalista_home(city=city, state=state)
        pr.segment = GENERALIST_NICHE
        extra = dict(pr.extra or {})
        extra["review_origin_niche"] = origin_niche
        extra["review_routed"] = GENERALIST_NICHE
        if score.get("reason"):
            extra["review_niche_drop"] = {
                "reason": score.get("reason"),
                "score": score.get("score"),
            }
        pr.extra = extra
        result = await self._persist(
            home,
            pr,
            score_g,
            origin_niche=origin_niche,
            routed=True,
        )
        if result.get("outcome") == "keep":
            result["outcome"] = "keep_geral"
            result["reason"] = (
                f"fora do nicho {origin_niche}, cabe no geral"
                + (f" ({score_g.get('reason')})" if score_g.get("reason") else "")
            )[:120]
        return result

    @staticmethod
    def _fits_generalista(pr: ProviderResult) -> bool:
        prev = pr.segment
        pr.segment = GENERALIST_NICHE
        ok = pr.is_valid_company()
        pr.segment = prev
        return ok

    @staticmethod
    def _is_keep(score: dict[str, Any], min_score: int) -> bool:
        keep = bool(score.get("keep", True))
        pts = int(score.get("score") or 0)
        return keep and pts >= min_score

    @staticmethod
    def _hard_drop(reason: str) -> bool:
        r = (reason or "").lower()
        return any(m in r for m in _HARD_DROP_MARKERS)

    async def _score(
        self,
        pr: ProviderResult,
        *,
        niche: str,
        city: str,
        force: bool,
    ) -> dict[str, Any]:
        return await score_company_candidate(
            name=pr.company_name or "",
            website=pr.website or "",
            snippet=str((pr.extra or {}).get("snippet") or ""),
            niche=niche,
            city=city,
            email=pr.email or "",
            stage="review",
            force=force,
        )

    async def _persist(
        self,
        campaign: Campaign,
        pr: ProviderResult,
        score: dict[str, Any],
        *,
        origin_niche: str,
        routed: bool = False,
    ) -> dict[str, Any]:
        extra = dict(pr.extra or {})
        extra["llm_score"] = {
            "score": score.get("score"),
            "reason": score.get("reason"),
            "confidence": score.get("confidence"),
        }
        pr.extra = extra
        cn = (score.get("clean_name") or "").strip()
        if cn and len(cn) > 2:
            pr.company_name = cn[:191]
        created = 0
        campaign_id = campaign.id
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                created = await self.stages.persist_discovered(campaign, [pr])
                await self.session.commit()
                last_exc = None
                break
            except OperationalError as exc:
                last_exc = exc
                if not _is_lock_timeout(exc) or attempt >= 2:
                    raise
                logger.warning(
                    "review_persist_lock_wait",
                    attempt=attempt + 1,
                    name=pr.company_name,
                )
                await self.session.rollback()
                await asyncio.sleep(0.4 * (2 ** attempt))
                reloaded = await self.stages.get_campaign(campaign_id)
                if reloaded is None:
                    raise
                campaign = reloaded
        if last_exc is not None:
            raise last_exc
        pts = int(score.get("score") or 0)
        logger.info(
            "review_keep_geral" if routed else "review_keep",
            name=pr.company_name,
            score=pts,
            created=created,
            niche=campaign.niche,
            origin_niche=origin_niche,
        )
        return {
            "outcome": "keep" if created else "duplicate",
            "reason": str(score.get("reason") or "ok"),
            "name": pr.company_name,
            "score": pts,
            "created": created,
            "campaign_id": campaign.id,
            "routed": routed,
        }

    async def _drop(
        self,
        payload: dict[str, Any],
        pr: ProviderResult,
        score: dict[str, Any],
        origin_niche: str,
    ) -> dict[str, Any]:
        await self.queue.forget(str(payload.get("dedup_key") or ""))
        pts = int(score.get("score") or 0)
        logger.info(
            "review_drop",
            name=pr.company_name,
            reason=score.get("reason"),
            score=pts,
            niche=origin_niche,
        )
        return {
            "outcome": "drop",
            "reason": str(score.get("reason") or "llm_drop"),
            "name": pr.company_name or "",
            "score": pts,
        }

    async def _generalista_home(self, *, city: str, state: str) -> Campaign:
        """Campanha generalista da mesma cidade, ou cria overflow."""
        city = (city or "").strip()
        state = (state or "").strip()
        base = (
            select(Campaign)
            .where(func.lower(Campaign.niche) == GENERALIST_NICHE)
            .order_by(Campaign.created_at.desc())
        )
        if city:
            hit = (
                await self.session.execute(base.where(Campaign.city == city).limit(1))
            ).scalar_one_or_none()
            if hit:
                return hit
        hit = (await self.session.execute(base.limit(1))).scalar_one_or_none()
        if hit:
            return hit
        label = f"Generalista overflow {city} {state}".strip()[:255] or "Generalista overflow"
        camp = Campaign(
            id=uuid4().hex,
            name=label,
            niche=GENERALIST_NICHE,
            provider="generalista",
            query="review-overflow",
            city=city or None,
            state=state or None,
            max_results=20,
            status=CampaignStatus.RUNNING.value,
            current_stage="discover",
            config={
                "skip_email": True,
                "origin": "review_overflow",
                "generalista": True,
            },
        )
        self.session.add(camp)
        await self.session.flush()
        logger.info("review_overflow_campaign", campaign_id=camp.id, city=city)
        return camp


def review_live_line(result: dict[str, Any]) -> str:
    name = (result.get("name") or "")[:40]
    outcome = result.get("outcome") or ""
    score = result.get("score")
    bit = f"{outcome} {name}".strip()
    if score is not None:
        bit += f"  score {score}"
    if result.get("reason"):
        bit += f"  {result['reason']}"
    return bit[:110]
