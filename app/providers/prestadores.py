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
        from app.domain.cities import search_location

        q = ctx.query or "prestador de serviços"
        loc = search_location(ctx.city, ctx.state)
        round_idx = int((ctx.extra or {}).get("discover_round") or 0)
        extras = (
            f"escritório de contabilidade {loc}",
            f"consultoria empresarial {loc}",
            f"assessoria fiscal {loc}",
            f"recursos humanos empresa {loc}",
            f"engenharia consultoria {loc}",
        )
        maps_q = build_maps_query(q, ctx.city, ctx.state)
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

        places = await serper_search(
            maps_q,
            num=max(ctx.max_results, 10),
            search_type="places",
            city=ctx.city,
            state=ctx.state,
        )
        for item in places:
            _add(
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
            if len(results) >= ctx.max_results:
                break

        queries = [maps_q, extras[round_idx % len(extras)]]
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
                r.source = "google_search"
                _add(r)

        return results[: ctx.max_results]
