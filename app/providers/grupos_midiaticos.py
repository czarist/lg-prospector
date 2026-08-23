"""Provider: Grupos Midiáticos — Google Search, News, Expediente, Contato, Publicidade."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.domain_email import extract_registrable_domain
from app.providers.geo_email import is_known_foreign_host, site_origin
from app.providers.http_tools import serper_search

_BACKEND_SOURCES = frozenset(
    {"serper", "bing", "duckduckgo", "duckduckgo_news", "google_news", "openstreetmap"}
)


def _outlet_from_news(item: dict) -> tuple[str, str]:
    """Veículo (fonte), não o título da matéria. Site = origem, não o artigo."""
    publisher = (
        item.get("publisher") or item.get("source") or item.get("name") or ""
    ).strip()
    if publisher.lower() in _BACKEND_SOURCES:
        publisher = ""
    link = (item.get("link") or item.get("website") or item.get("url") or "").strip()
    origin = site_origin(link)
    if is_known_foreign_host(origin or link):
        return "", ""
    if publisher:
        return publisher, origin or link
    host = extract_registrable_domain(origin or link)
    if not host or is_known_foreign_host(host):
        return "", ""
    label = host.split(".")[0].replace("-", " ").strip()
    return (label.title() if label else ""), origin or link


class GruposMidiaticosProvider(SearchProviderMixin, BaseProvider):
    code = "grupos_midiaticos"
    name = "Grupos Midiáticos"
    niche = "grupo_midiatico"
    template_file = "email-prospeccao-jornalismo.html"
    strategies = ["google_search", "google_news", "expediente", "contato", "publicidade"]
    segment = "grupo_midiatico"
    source_label = "google_news"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        from app.domain.cities import search_location

        q = ctx.query or "jornal grupo de mídia comunicação"
        loc = search_location(ctx.city, ctx.state)
        round_idx = int((ctx.extra or {}).get("discover_round") or 0)
        extras = (
            f"{q} publicidade comercial contato {loc}".strip(),
            f"portal de notícias brasileiro {loc} contato comercial",
            f"rádio jornal {loc} publicidade",
            f"tv regional {loc} comercial",
            f"revista regional {loc} anuncie",
        )
        results: list[ProviderResult] = []
        seen: set[str] = set()

        def _add(pr: ProviderResult) -> None:
            if not pr.is_valid_company() or self._is_known_lead(pr, ctx):
                return
            key = pr.normalize_key()
            if key in seen:
                return
            seen.add(key)
            results.append(pr)

        news_q = f"{q} {loc}".strip() if loc else q
        news = await serper_search(
            news_q, num=max(ctx.max_results, 10), search_type="news", city=ctx.city, state=ctx.state
        )
        for item in news:
            name, origin = _outlet_from_news(item)
            if not name or not origin:
                continue
            _add(
                ProviderResult(
                    company_name=self._clean_title(name),
                    website=origin,
                    city=ctx.city,
                    state=ctx.state,
                    segment=self.segment,
                    source="google_news",
                    extra={"snippet": item.get("snippet"), "raw": item},
                )
            )
            if len(results) >= ctx.max_results:
                break

        for qq in (extras[round_idx % len(extras)], extras[(round_idx + 2) % len(extras)]):
            if len(results) >= ctx.max_results:
                break
            more = await self._search_organic(
                qq,
                max(8, ctx.max_results - len(results)),
                city=ctx.city,
                state=ctx.state,
                ctx=ctx,
            )
            for r in more:
                r.segment = self.segment
                r.source = "google_search"
                _add(r)

        return results[: ctx.max_results]
