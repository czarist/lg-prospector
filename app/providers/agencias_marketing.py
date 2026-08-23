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
        from app.domain.cities import search_location

        q = ctx.query or "agência de marketing"
        loc = search_location(ctx.city, ctx.state)
        round_idx = int((ctx.extra or {}).get("discover_round") or 0)
        variants = [
            f"site:linkedin.com/company {q} {loc}".strip(),
            build_maps_query(f"{q} digital", ctx.city, ctx.state),
            f"agência de publicidade {loc} site contato",
            f"agência inbound marketing {loc}",
            f"agência social media {loc} contato",
            f"estúdio branding design {loc}",
        ]
        start = round_idx % len(variants)
        queries = [variants[(start + i) % len(variants)] for i in range(4)]
        results: list[ProviderResult] = []
        seen: set[str] = set()
        for qq in queries:
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
                r.source = r.source or "linkedin"
                if not r.is_valid_company() or self._is_known_lead(r, ctx):
                    continue
                key = r.normalize_key()
                if key in seen:
                    continue
                seen.add(key)
                results.append(r)
        return results[: ctx.max_results]
