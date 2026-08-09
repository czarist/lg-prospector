"""Provider: Prestadores de Serviço — Google Search, Google Maps."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import build_maps_query, serper_search


class PrestadoresProvider(SearchProviderMixin, BaseProvider):
    code = "prestadores_servico"
    name = "Prestadores de Serviço"
    niche = "prestador_servico"
    template_file = "email-prospeccao-prestadores.html"
    strategies = ["google_search", "google_maps"]
    segment = "prestador_servico"
    source_label = "google_maps"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "prestador de serviços"
        maps_q = build_maps_query(q, ctx.city, ctx.state)
        results: list[ProviderResult] = []

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
            more = await self._search_organic(
                maps_q, ctx.max_results - len(results), city=ctx.city, state=ctx.state
            )
            for r in more:
                r.segment = self.segment
                r.source = "google_search"
            results.extend(more)

        return [r for r in results if r.is_valid_company()][: ctx.max_results]
