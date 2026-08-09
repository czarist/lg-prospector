"""Provider: Políticos / Partidos — mesmo nicho institucional.

Fontes: bases públicas, Câmara, Senado, sites oficiais, diretórios.
Template e assunto únicos: email-prospeccao-politicos.html
"""

from __future__ import annotations

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.http_tools import serper_search


class PoliticosProvider(SearchProviderMixin, BaseProvider):
    code = "politicos"
    name = "Políticos e Partidos"
    niche = "politico"  # canônico; "partido" é alias no registry
    template_file = "email-prospeccao-politicos.html"
    strategies = [
        "bases_publicas",
        "portal_camara",
        "portal_senado",
        "sites_oficiais",
        "diretorios_estaduais",
        "diretorios_municipais",
    ]
    segment = "politico"
    source_label = "bases_publicas"

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        q = ctx.query or "deputado federal"
        results: list[ProviderResult] = []
        seen: set[str] = set()

        queries = [
            f"site:camara.leg.br {q}",
            f"site:senado.leg.br {q}",
            f"{q} gabinete contato oficial",
            f"partido político {q} diretório site oficial contato".strip(),
        ]
        if ctx.state:
            queries.append(f"{q} {ctx.state} contato oficial")
            queries.append(f"diretório estadual partido {ctx.state} contato")
        if ctx.city:
            queries.append(f"diretório municipal partido {ctx.city} contato")

        for query in queries:
            if len(results) >= ctx.max_results:
                break
            organic = await serper_search(
                query, num=ctx.max_results, city=ctx.city, state=ctx.state
            )
            for item in organic:
                title = self._clean_title(item.get("title") or "")
                link = item.get("link") or ""
                key = f"{title}|{link}"
                if key in seen or not title:
                    continue
                seen.add(key)
                source = "sites_oficiais" if "partido" in query.lower() or "diretório" in query.lower() else "bases_publicas"
                results.append(
                    ProviderResult(
                        company_name=title,
                        contact_name=title,
                        website=link,
                        city=ctx.city,
                        state=ctx.state,
                        segment=self.segment,
                        source=source,
                        extra={"snippet": item.get("snippet"), "raw": item},
                    )
                )
                if len(results) >= ctx.max_results:
                    break

        return results[: ctx.max_results]
