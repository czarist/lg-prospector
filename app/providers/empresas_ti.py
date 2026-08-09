"""Provider: Empresas de TI — LinkedIn, Website, Firecrawl, Playwright."""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import build_maps_query


class EmpresasTIProvider(SearchProviderMixin, BaseProvider):
    code = "empresas_ti"
    name = "Empresas de TI"
    niche = "empresa_ti"
    template_file = "email-prospeccao-empresas-ti.html"
    strategies = ["linkedin", "website", "local_scrape", "playwright"]
    segment = "empresa_ti"
    source_label = "linkedin"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "software house desenvolvimento"
        loc = " ".join(x for x in [ctx.city, ctx.state] if x).strip()
        # poucas queries por rodada (evita cascata DDG × N)
        queries = [
            f"{q} {loc}".strip(),
            f"software house {loc} desenvolvimento".strip(),
            f"fábrica de software {loc}".strip(),
        ]
        results: list[ProviderResult] = []
        seen: set[str] = set()
        for qq in queries:
            if len(results) >= ctx.max_results:
                break
            need = max(6, ctx.max_results - len(results) + 2)
            more = await self._search_organic(
                qq, need, city=ctx.city, state=ctx.state
            )
            for r in more:
                r.segment = self.segment
                if not r.is_valid_company():
                    continue
                k = r.normalize_key()
                if k in seen:
                    continue
                seen.add(k)
                results.append(r)
        return results[: ctx.max_results]
