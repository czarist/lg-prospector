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

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class _RetryableSerper(Exception):
    """5xx / 429 — vale retry. 4xx permanente não entra aqui."""


def _serper_key() -> str:
    settings = get_settings()
    return (settings.serper_api_key or "").strip().strip('"').strip("'")


def _resolve_backend() -> str:
    """
    free | serper | auto
    auto: Serper se tiver key, senão free.
    """
    settings = get_settings()
    backend = (getattr(settings, "search_backend", None) or "auto").lower().strip()
    key = _serper_key()
    if backend == "auto":
        return "serper" if key else "free"
    if backend == "serper":
        return "serper"
    if backend == "free":
        return "free"
    return "serper" if key else "free"


def _extract_serper_hits(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("organic", "places", "news"):
        hits = data.get(key)
        if isinstance(hits, list) and hits:
            out: list[dict[str, Any]] = []
            for item in hits:
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("source", "serper")
                    out.append(row)
            return out
    return []


async def serper_search_raw(
    query: str, num: int = 10, search_type: str = "search"
) -> list[dict[str, Any]]:
    """Busca via Serper.dev (requer SERPER_API_KEY). Sem fallback."""
    key = _serper_key()
    if not key:
        logger.error("serper_missing_key", query=query[:80], search_type=search_type)
        return []

    url = f"https://google.serper.dev/{search_type}"
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": max(1, min(int(num or 10), 100)), "gl": "br", "hl": "pt-br"}

    async def _do():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in _RETRYABLE_STATUS:
                raise _RetryableSerper(
                    f"status={resp.status_code} body={resp.text[:200]}"
                )
            if resp.status_code >= 400:
                logger.error(
                    "serper_http_error",
                    status=resp.status_code,
                    body=resp.text[:300],
                    query=query[:80],
                    search_type=search_type,
                )
                return []
            return resp.json()

    try:
        data = await async_retry(
            _do, attempts=2, exceptions=(_RetryableSerper, httpx.TransportError)
        )
    except Exception as exc:
        logger.error("serper_error", error=str(exc), query=query[:80], search_type=search_type)
        return []

    hits = _extract_serper_hits(data)
    logger.info(
        "serper_ok",
        query=query[:80],
        search_type=search_type,
        count=len(hits),
        credits=(data.get("credits") if isinstance(data, dict) else None),
    )
    return hits


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
    - serper: só Serper. Sem key / erro / vazio → lista vazia (não cai no DDG).
    - auto: Serper se houver key, senão free. Com key também não cai no DDG.
    """
    backend = _resolve_backend()
    logger.info(
        "web_search",
        backend=backend,
        search_type=search_type,
        query=query[:80],
        city=city or None,
        state=state or None,
        serper_key=bool(_serper_key()),
    )

    if backend == "serper":
        return await serper_search_raw(query, num=num, search_type=search_type)

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
