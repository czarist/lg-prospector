"""Fila Redis: caçada enfileira candidato; reviewer valida na IA local.

A caçada não espera o Qwen. O reviewer (processo à parte) consome,
pontua e só então pede a gravação — ou descarta.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.redis.client import get_redis
from app.providers.domain_email import extract_registrable_domain

logger = get_logger(__name__)


def candidate_dedup_key(
    *,
    niche: str = "",
    name: str = "",
    website: str = "",
    email: str = "",
) -> str:
    host = (extract_registrable_domain(website or "") or "").lower()
    em = (email or "").strip().lower()
    n = (name or "").strip().lower()
    return f"{(niche or '').lower()}|{host}|{n}|{em}"


class ReviewQueue:
    def __init__(self, name: str | None = None) -> None:
        settings = get_settings()
        self.name = (name or settings.review_queue_name).strip() or "lg:review"
        self.seen_key = f"{self.name}:seen"
        self.ttl = max(3600, int(settings.review_seen_ttl_seconds))

    async def enqueue(self, payload: dict[str, Any]) -> bool:
        """True se entrou na fila; False se já vimos este candidato."""
        r = await get_redis()
        key = str(payload.get("dedup_key") or "")
        if key:
            added = await r.sadd(self.seen_key, key)
            if not added:
                return False
            await r.expire(self.seen_key, self.ttl)
        payload.setdefault("enqueued_at", datetime.now(timezone.utc).isoformat())
        await r.rpush(self.name, json.dumps(payload, default=str))
        return True

    async def dequeue(self, timeout: int = 2) -> Optional[dict[str, Any]]:
        r = await get_redis()
        if timeout > 0:
            item = await r.blpop(self.name, timeout=timeout)
            if item is None:
                return None
            _, raw = item
        else:
            raw = await r.lpop(self.name)
            if raw is None:
                return None
        try:
            data = json.loads(raw)
        except Exception:
            logger.warning("review_queue_bad_payload")
            return None
        return data if isinstance(data, dict) else None

    async def requeue(self, payload: dict[str, Any]) -> None:
        r = await get_redis()
        await r.lpush(self.name, json.dumps(payload, default=str))

    async def length(self) -> int:
        r = await get_redis()
        return int(await r.llen(self.name))

    async def forget(self, dedup_key: str) -> None:
        """Libera a chave se o candidato foi dropado (pode voltar noutro SERP)."""
        if not dedup_key:
            return
        r = await get_redis()
        await r.srem(self.seen_key, dedup_key)
