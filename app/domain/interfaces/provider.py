"""Interface de providers de prospecção por nicho."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.domain.entities.provider_result import ProviderResult


@dataclass
class SearchContext:
    query: str = ""
    city: str = ""
    state: str = ""
    max_results: int = 20
    extra: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Cada nicho implementa um provider concreto."""

    code: str
    name: str
    niche: str
    template_file: str
    strategies: list[str]

    @abstractmethod
    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        """Pesquisa empresas no nicho."""

    async def find_contacts(self, company: ProviderResult, ctx: SearchContext) -> list[ProviderResult]:
        """Encontra contatos para uma empresa. Default: retorna o próprio registro se já tiver contato."""
        if company.contact_name or company.email:
            return [company]
        return []

    async def find_emails(self, contact: ProviderResult, ctx: SearchContext) -> ProviderResult:
        """Enriquece e-mail do contato. Default: sem alteração."""
        return contact
