"""Registry de providers por nicho — desacoplado do LangGraph."""

from __future__ import annotations

from typing import Optional

from app.domain.interfaces.provider import BaseProvider
from app.providers.advogados import AdvogadosProvider
from app.providers.agencias_marketing import AgenciasMarketingProvider
from app.providers.empresas_ti import EmpresasTIProvider
from app.providers.generalista import GeneralistaProvider
from app.providers.grupos_midiaticos import GruposMidiaticosProvider
from app.providers.politicos import PoliticosProvider
from app.providers.prestadores import PrestadoresProvider

# Mapeamento canônico niche → template
# generalista NÃO entra no hunt_loop de nicho (DEFAULT_NICHES)
NICHE_TEMPLATES: dict[str, str] = {
    "advogado": "email-prospeccao-advogados.html",
    "agencia_marketing": "email-prospeccao-agencias.html",
    "empresa_ti": "email-prospeccao-empresas-ti.html",
    "prestador_servico": "email-prospeccao-prestadores.html",
    "grupo_midiatico": "email-prospeccao-jornalismo.html",
    "politico": "email-prospeccao-politicos.html",
    "generalista": "email-prospeccao-generalista.html",
}

# Aliases → nicho canônico (partido = politico)
NICHE_ALIASES: dict[str, str] = {
    "partido": "politico",
    "partidos": "politico",
    "politicos": "politico",
}


def canonicalize_niche(niche: str) -> str:
    key = (niche or "").strip().lower()
    return NICHE_ALIASES.get(key, key)


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._by_niche: dict[str, BaseProvider] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        for provider in (
            AdvogadosProvider(),
            AgenciasMarketingProvider(),
            EmpresasTIProvider(),
            PrestadoresProvider(),
            GruposMidiaticosProvider(),
            PoliticosProvider(),
            GeneralistaProvider(),
        ):
            self.register(provider)

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.code] = provider
        self._by_niche[provider.niche] = provider

    def get(self, code: str) -> BaseProvider:
        # code "partidos" legado → politicos
        code = NICHE_ALIASES.get(code, code)
        if code == "politico":
            code = "politicos"
        if code not in self._providers:
            raise KeyError(f"Provider desconhecido: {code}")
        return self._providers[code]

    def get_by_niche(self, niche: str) -> BaseProvider:
        niche = canonicalize_niche(niche)
        if niche not in self._by_niche:
            raise KeyError(f"Nenhum provider para o nicho: {niche}")
        return self._by_niche[niche]

    def resolve(self, niche_or_code: str) -> BaseProvider:
        key = canonicalize_niche(niche_or_code)
        if key in self._providers:
            return self._providers[key]
        if key in self._by_niche:
            return self._by_niche[key]
        # code politicos
        if niche_or_code in self._providers:
            return self._providers[niche_or_code]
        raise KeyError(f"Provider/nicho desconhecido: {niche_or_code}")

    def list_providers(self) -> list[dict]:
        return [
            {
                "code": p.code,
                "name": p.name,
                "niche": p.niche,
                "template_file": p.template_file,
                "strategies": p.strategies,
                "aliases": [a for a, n in NICHE_ALIASES.items() if n == p.niche],
            }
            for p in self._providers.values()
        ]

    def list_niches(self) -> list[str]:
        return list(NICHE_TEMPLATES.keys())

    def template_for_niche(self, niche: str) -> str:
        niche = canonicalize_niche(niche)
        if niche in NICHE_TEMPLATES:
            return NICHE_TEMPLATES[niche]
        provider = self._by_niche.get(niche) or self._providers.get(niche)
        if provider:
            return provider.template_file
        raise KeyError(f"Template não encontrado para nicho: {niche}")


_registry: Optional[ProviderRegistry] = None


def get_provider_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
