"""Provider: Agências de Marketing — LinkedIn, Website, Firecrawl."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import build_maps_query


class AgenciasMarketingProvider(SearchProviderMixin, BaseProvider):
    code = "agencias_marketing"
    name = "Agências de Marketing"
    niche = "agencia_marketing"
    template_file = "email-prospeccao-agencias.html"
    strategies = ["linkedin", "website", "local_scrape"]
    segment = "agencia_marketing"
    source_label = "linkedin"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "agência de marketing"
        li_q = f'site:linkedin.com/company {q}'
        if ctx.city:
            li_q += f" {ctx.city}"
        results = await self._search_organic(
            li_q, ctx.max_results, city=ctx.city, state=ctx.state
        )

        if len(results) < ctx.max_results:
            web_q = build_maps_query(f"{q} digital", ctx.city, ctx.state)
            more = await self._search_organic(
                web_q, ctx.max_results - len(results), city=ctx.city, state=ctx.state
            )
            results.extend(more)

        for r in results:
            r.segment = self.segment
            r.source = r.source or "linkedin"
        return [r for r in results if r.is_valid_company()][: ctx.max_results]
