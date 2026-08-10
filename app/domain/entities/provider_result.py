"""Resultado padronizado de todos os providers de prospecção."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderResult(BaseModel):
    company_name: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    city: str = ""
    state: str = ""
    segment: str = ""
    source: str = ""
    extra: dict = Field(default_factory=dict)

    # Domínios genéricos / conteúdo que não são empresas-alvo B2B
    _JUNK_HOST_PARTS = (
        "wikipedia.org",
        "wikimedia.org",
        "youtube.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "tiktok.com",
        "microsoft.com",
        "apple.com",
        "google.com",
        "amazon.com",
        "gov.br",  # portais públicos genéricos (exceto se nicho for politico)
        "todamateria.com.br",
        "significados.com.br",
        "brasil.esquilo.com",
        "pt.wikipedia",
    )

    # nichos que buscam de propósito nessas fontes (spec.md) — não é "lixo" pra eles
    _JUNK_HOST_ALLOW_BY_SEGMENT = {
        "linkedin.com": {"agencia_marketing", "empresa_ti"},
    }

    def normalize_key(self) -> str:
        """Chave para deduplicação."""
        name = (self.company_name or "").strip().lower()
        website = (self.website or "").strip().lower().rstrip("/")
        city = (self.city or "").strip().lower()
        return f"{name}|{website}|{city}"

    def is_valid_company(self) -> bool:
        name = (self.company_name or "").strip()
        if not name:
            return False
        # títulos de artigo/blog costumam ter ":", "?" ou ser muito longos
        if len(name) > 120:
            return False
        if any(x in name for x in ("?", " o que é", "o que e ", "conheça os", "tipos e")):
            return False
        host = (self.website or "").lower()
        # politico / partido pode ter gov.br — demais nichos filtram
        if self.segment not in {"politico", "partido"}:
            for junk in self._JUNK_HOST_PARTS:
                if junk in host and self.segment not in self._JUNK_HOST_ALLOW_BY_SEGMENT.get(
                    junk, set()
                ):
                    return False
        return True
