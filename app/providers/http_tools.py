"""Ferramentas HTTP compartilhadas: busca (free/serper) + scrape local."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infrastructure.resilience.retry import async_retry
from app.providers.scraper import scrape_page_text, scrape_website
from app.providers.search_free import free_search

logger = get_logger(__name__)


def _resolve_backend() -> str:
    """
    free | serper | auto
    auto: Serper se tiver key, senão free.
    """
    settings = get_settings()
    backend = (getattr(settings, "search_backend", None) or "auto").lower().strip()
    if backend == "auto":
        return "serper" if settings.serper_api_key else "free"
    if backend in {"free", "serper"}:
        return backend
    return "free"


async def serper_search_raw(
    query: str, num: int = 10, search_type: str = "search"
) -> list[dict[str, Any]]:
    """Busca via Serper.dev (requer SERPER_API_KEY)."""
    settings = get_settings()
    if not settings.serper_api_key:
        return []

    url = f"https://google.serper.dev/{search_type}"
    headers = {"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "gl": "br", "hl": "pt-br"}

    async def _do():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    try:
        data = await async_retry(_do, attempts=2, exceptions=(httpx.HTTPError,))
    except Exception as exc:
        logger.error("serper_error", error=str(exc), query=query)
        return []

    organic = data.get("organic") or data.get("places") or data.get("news") or []
    return organic if isinstance(organic, list) else []


async def web_search(
    query: str,
    num: int = 10,
    search_type: str = "search",
    *,
    city: str = "",
    state: str = "",
) -> list[dict[str, Any]]:
    """
    Busca unificada.

    SEARCH_BACKEND=auto|free|serper
    - free: DuckDuckGo + Overpass/OSM
    - serper: Serper (se sem key, cai no free)
    - auto: Serper se houver key, senão free
    """
    backend = _resolve_backend()
    logger.info(
        "web_search",
        backend=backend,
        search_type=search_type,
        query=query[:80],
        city=city or None,
        state=state or None,
    )

    if backend == "serper":
        results = await serper_search_raw(query, num=num, search_type=search_type)
        if results:
            return results
        logger.warning("serper_empty_fallback_free", query=query)
        return await free_search(
            query, num=num, search_type=search_type, city=city, state=state
        )

    return await free_search(
        query, num=num, search_type=search_type, city=city, state=state
    )


async def serper_search(
    query: str,
    num: int = 10,
    search_type: str = "search",
    *,
    city: str = "",
    state: str = "",
) -> list[dict[str, Any]]:
    """Alias compatível — todos os providers usam web_search por baixo."""
    return await web_search(
        query, num=num, search_type=search_type, city=city, state=state
    )


async def local_scrape(url: str) -> Optional[str]:
    """Scraping local (httpx + BS4 + Playwright). Substitui Firecrawl."""
    return await scrape_page_text(url)


async def local_scrape_contacts(url: str):
    """Retorna ScrapeResult com e-mails/telefones ranqueados."""
    return await scrape_website(url)


async def firecrawl_scrape(url: str) -> Optional[str]:
    """Deprecated: redireciona para scraper local."""
    return await local_scrape(url)


async def simple_get_text(url: str, timeout: float = 20.0) -> Optional[str]:
    from app.providers.scraper import fetch_httpx

    return await fetch_httpx(url, timeout=timeout)


def build_maps_query(query: str, city: str = "", state: str = "") -> str:
    parts = [query]
    if city:
        parts.append(city)
    if state:
        parts.append(state)
    parts.append("Brasil")
    return " ".join(parts)


def google_search_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"
