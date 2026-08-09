"""Rate limiting via Redis (sliding window com sorted set)."""

from __future__ import annotations

import time
import uuid

from app.core.config import get_settings
from app.infrastructure.redis.client import get_redis


class RateLimiter:
    def __init__(
        self,
        key_prefix: str = "ratelimit",
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        settings = get_settings()
        self.key_prefix = key_prefix
        self.max_requests = max_requests or settings.rate_limit_requests
        self.window_seconds = window_seconds or settings.rate_limit_window_seconds

    def _key(self, identity: str) -> str:
        return f"{self.key_prefix}:{identity}"

    async def is_allowed(self, identity: str) -> bool:
        r = await get_redis()
        key = self._key(identity)
        now = time.time()
        window_start = now - self.window_seconds
        member = f"{now}:{uuid.uuid4().hex}"

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, self.window_seconds + 1)
        results = await pipe.execute()
        count = results[2]
        return int(count) <= self.max_requests

    async def acquire(self, identity: str) -> bool:
        """Retorna True se permitido; False se limite excedido."""
        return await self.is_allowed(identity)
