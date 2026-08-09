"""Utilitários e comportamento comum de providers."""

from __future__ import annotations

import re

from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.email_enrichment import require_email
from app.providers.http_tools import serper_search
from app.providers.scraper import extract_emails


class SearchProviderMixin:
    """Mixin com helpers de pesquisa/enriquecimento."""

    segment: str = ""
    source_label: str = "search"

    async def _search_organic(
        self,
        query: str,
        max_results: int,
        *,
        city: str = "",
        state: str = "",
        search_type: str = "search",
    ) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        organic = await serper_search(
            query,
            num=max_results,
            search_type=search_type,
            city=city,
            state=state,
        )
        for item in organic[:max_results]:
            title = item.get("title") or item.get("name") or ""
            link = item.get("link") or item.get("website") or item.get("url") or ""
            snippet = item.get("snippet") or item.get("description") or ""
            phone = item.get("phoneNumber") or item.get("phone") or ""
            address = item.get("address") or ""
            email = (item.get("email") or "").strip()
            parsed_city, parsed_state = self._parse_location(address)
            if not email:
                emails = extract_emails(snippet)
                email = emails[0] if emails else ""
            results.append(
                ProviderResult(
                    company_name=self._clean_title(title),
                    website=link,
                    phone=phone,
                    email=email,
                    city=parsed_city or city,
                    state=parsed_state or state,
                    segment=self.segment,
                    source=item.get("source") or self.source_label,
                    extra={"snippet": snippet, "raw": item},
                )
            )
        return results

    async def _enrich_from_website(self, company: ProviderResult) -> ProviderResult:
        """Enriquece e-mail (multi-pass). Sem e-mail ainda retorna o registro (filter depois)."""
        from app.providers.email_enrichment import find_email_multi_pass

        return await find_email_multi_pass(company, deep=True)

    @staticmethod
    def _clean_title(title: str) -> str:
        for sep in [" - ", " | ", " – "]:
            if sep in title:
                title = title.split(sep)[0]
        return title.strip()

    @staticmethod
    def _parse_location(address: str) -> tuple[str, str]:
        if not address:
            return "", ""
        city, state = "", ""
        m = re.search(r"([A-Za-zÀ-ú\s]+)\s*-\s*([A-Z]{2})\b", address)
        if m:
            city = m.group(1).strip().split(",")[-1].strip()
            state = m.group(2).strip()
        return city, state

    async def find_contacts(self, company: ProviderResult, ctx: SearchContext) -> list[ProviderResult]:
        """Enriquece; se não achar e-mail após multi-pass, descarta (lista vazia)."""
        base = ProviderResult(
            **{
                **company.model_dump(),
                "contact_name": company.contact_name
                or (f"Contato {company.company_name}" if company.company_name else ""),
            }
        )
        kept = await require_email(base, deep=True)
        return [kept] if kept else []

    async def find_emails(self, contact: ProviderResult, ctx: SearchContext) -> ProviderResult:
        """Multi-pass e-mail. Sem e-mail: retorna contact com email vazio (ValidateLead rejeita)."""
        from app.providers.email_enrichment import find_email_multi_pass

        return await find_email_multi_pass(contact, deep=True)
