"""Limites de concorrência da caçada — protege CPU/RAM e a IA local.

A busca free (DDG/Bing/OSM) + Playwright pesam na máquina.
O LLM (LiteLLM/Qwen) só é usado se HUNT_USE_LLM=true e com concurrency=1.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.core.config import get_settings

_search_sem: Optional[asyncio.Semaphore] = None
_scrape_sem: Optional[asyncio.Semaphore] = None
_playwright_sem: Optional[asyncio.Semaphore] = None
_llm_sem: Optional[asyncio.Semaphore] = None
_enrich_sem: Optional[asyncio.Semaphore] = None

_last_search_at: float = 0.0
_search_lock: Optional[asyncio.Lock] = None


def _sem(attr: str, n: int) -> asyncio.Semaphore:
    """Cria semáforo lazy por event-loop (evita reuso entre loops de testes)."""
    # recria se n mudou ou ainda não existe
    current = globals().get(attr)
    if current is None or getattr(current, "_limit", None) != n:
        sem = asyncio.Semaphore(max(1, n))
        sem._limit = n  # type: ignore[attr-defined]
        globals()[attr] = sem
        return sem
    return current  # type: ignore[return-value]


def search_semaphore() -> asyncio.Semaphore:
    return _sem("_search_sem", get_settings().search_concurrency)


def scrape_semaphore() -> asyncio.Semaphore:
    return _sem("_scrape_sem", get_settings().scrape_concurrency)


def playwright_semaphore() -> asyncio.Semaphore:
    return _sem("_playwright_sem", get_settings().playwright_concurrency)


def llm_semaphore() -> asyncio.Semaphore:
    return _sem("_llm_sem", get_settings().llm_concurrency)


def enrich_semaphore() -> asyncio.Semaphore:
    return _sem("_enrich_sem", get_settings().enrich_concurrency)


async def throttle_search() -> None:
    """Intervalo mínimo entre buscas free (evita ban e picos de CPU)."""
    global _last_search_at, _search_lock
    settings = get_settings()
    interval = max(0.0, settings.search_min_interval_seconds)
    if interval <= 0:
        return
    if _search_lock is None:
        _search_lock = asyncio.Lock()
    async with _search_lock:
        now = time.monotonic()
        wait = interval - (now - _last_search_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_search_at = time.monotonic()
