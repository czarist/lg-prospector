"""Checkpoints do LangGraph em Redis (fallback simples)."""

from __future__ import annotations

import json
from typing import Any, Optional

from app.infrastructure.redis.client import get_redis


class RedisCheckpointStore:
    PREFIX = "lg:checkpoint:"

    async def save(self, thread_id: str, state: dict[str, Any], ttl: int = 86400 * 7) -> None:
        r = await get_redis()
        await r.set(f"{self.PREFIX}{thread_id}", json.dumps(state, default=str), ex=ttl)

    async def load(self, thread_id: str) -> Optional[dict[str, Any]]:
        r = await get_redis()
        raw = await r.get(f"{self.PREFIX}{thread_id}")
        if raw is None:
            return None
        return json.loads(raw)

    async def delete(self, thread_id: str) -> None:
        r = await get_redis()
        await r.delete(f"{self.PREFIX}{thread_id}")
