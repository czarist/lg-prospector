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
        "gov.br",
        "leg.br",
        "jus.br",
        "todamateria.com.br",
        "significados.com.br",
        "brasil.esquilo.com",
        "pt.wikipedia",
        "jusbrasil.com.br",  # perfil/conteúdo, não site do escritório
        "escavador.com",
        "indeed.com",
        "infojobs.com.br",
        "catho.com.br",
        "econodata.com.br",
        "juridicocerto.com",
        "previdenciarista.com",
        "ohub.com.br",
        "canaldoanuncio.com",
        "contaazul.com",
        "glassdoor.com",
        "lawzana.com",
        "cnpja.com",
        "cnpj.biz",
    )

    # nichos que buscam de propósito nessas fontes — não é "lixo" pra eles
    _JUNK_HOST_ALLOW_BY_SEGMENT = {
        "linkedin.com": {"agencia_marketing", "empresa_ti"},
        "gov.br": {"generalista"},
        "leg.br": {"generalista"},
        "jus.br": {"generalista"},
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
        if ":" in name and len(name) > 40:
            return False
        if any(x in name.lower() for x in ("?", " o que é", "o que e ", "conheça os", "tipos e", "wikipedia")):
            return False

        host = (self.website or "").lower()
        for junk in self._JUNK_HOST_PARTS:
            if junk in host and self.segment not in self._JUNK_HOST_ALLOW_BY_SEGMENT.get(
                junk, set()
            ):
                return False

        snippet = str((self.extra or {}).get("snippet") or "")
        from app.providers.geo_email import is_plausible_lead

        return is_plausible_lead(
            name=name,
            website=self.website or "",
            email=self.email or "",
            snippet=snippet,
            segment=(self.segment or "").lower(),
        )
