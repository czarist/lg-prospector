"""Fila simples baseada em Redis lists."""

from __future__ import annotations

import json
from typing import Any, Optional

from app.infrastructure.redis.client import get_redis


class JobQueue:
    def __init__(self, name: str = "lg:jobs") -> None:
        self.name = name

    async def enqueue(self, payload: dict[str, Any]) -> None:
        r = await get_redis()
        await r.rpush(self.name, json.dumps(payload, default=str))

    async def dequeue(self, timeout: int = 0) -> Optional[dict[str, Any]]:
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
        return json.loads(raw)

    async def length(self) -> int:
        r = await get_redis()
        return int(await r.llen(self.name))

    async def cache_get(self, key: str) -> Optional[str]:
        r = await get_redis()
        return await r.get(f"cache:{key}")

    async def cache_set(self, key: str, value: str, ttl: int = 3600) -> None:
        r = await get_redis()
        await r.set(f"cache:{key}", value, ex=ttl)
