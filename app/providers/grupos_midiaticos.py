"""Provider: Grupos Midiáticos — Google Search, News, Expediente, Contato, Publicidade."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import serper_search


class GruposMidiaticosProvider(SearchProviderMixin, BaseProvider):
    code = "grupos_midiaticos"
    name = "Grupos Midiáticos"
    niche = "grupo_midiatico"
    template_file = "email-prospeccao-jornalismo.html"
    strategies = ["google_search", "google_news", "expediente", "contato", "publicidade"]
    segment = "grupo_midiatico"
    source_label = "google_news"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "jornal grupo de mídia comunicação"
        results: list[ProviderResult] = []

        news_q = q
        if ctx.city:
            news_q = f"{q} {ctx.city}"
        news = await serper_search(
            news_q, num=ctx.max_results, search_type="news", city=ctx.city, state=ctx.state
        )
        for item in news[: ctx.max_results]:
            results.append(
                ProviderResult(
                    company_name=self._clean_title(item.get("title") or item.get("source") or ""),
                    website=item.get("link") or "",
                    city=ctx.city,
                    state=ctx.state,
                    segment=self.segment,
                    source="google_news",
                    extra={"snippet": item.get("snippet"), "raw": item},
                )
            )

        if len(results) < ctx.max_results:
            search_q = f"{q} publicidade comercial contato"
            if ctx.city:
                search_q += f" {ctx.city}"
            more = await self._search_organic(
                search_q, ctx.max_results - len(results), city=ctx.city, state=ctx.state
            )
            for r in more:
                r.segment = self.segment
                r.source = "google_search"
            results.extend(more)

        return [r for r in results if r.is_valid_company()][: ctx.max_results]
