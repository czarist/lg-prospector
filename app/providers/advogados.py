"""Provider: Advogados — Google Search, Maps, Website, Firecrawl. Sem scraping OAB em massa."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import build_maps_query, serper_search


class AdvogadosProvider(SearchProviderMixin, BaseProvider):
    code = "advogados"
    name = "Advogados"
    niche = "advogado"
    template_file = "email-prospeccao-advogados.html"
    strategies = ["google_search", "google_maps", "website", "local_scrape"]
    segment = "advogado"
    source_label = "google"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "escritório de advocacia"
        results: list[ProviderResult] = []

        maps_q = build_maps_query(f"{q} advogado", ctx.city, ctx.state)
        places = await serper_search(
            maps_q,
            num=ctx.max_results,
            search_type="places",
            city=ctx.city,
            state=ctx.state,
        )
        for item in places[: ctx.max_results]:
            results.append(
                ProviderResult(
                    company_name=item.get("title") or item.get("name") or "",
                    website=item.get("website") or item.get("link") or "",
                    phone=item.get("phoneNumber") or "",
                    city=ctx.city or self._parse_location(item.get("address") or "")[0],
                    state=ctx.state or self._parse_location(item.get("address") or "")[1],
                    segment=self.segment,
                    source="google_maps",
                    extra={"address": item.get("address"), "raw": item},
                )
            )

        if len(results) < ctx.max_results:
            search_q = build_maps_query(f"{q} escritório advocacia", ctx.city, ctx.state)
            more = await self._search_organic(
                search_q, ctx.max_results - len(results), city=ctx.city, state=ctx.state
            )
            for r in more:
                r.segment = self.segment
                r.source = "google_search"
            results.extend(more)

        return [r for r in results if r.is_valid_company()][: ctx.max_results]
