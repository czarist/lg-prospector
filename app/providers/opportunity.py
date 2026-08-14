"""Análise heurística de presença digital e oportunidade (rotina generalista).

Sem LLM: olha site, snippet e o que o scrape já trouxe. Escolhe 1–3
serviços plausíveis e uma frase curta para o e-mail.
Não inventa problema que não tenha sinal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.providers.geo_email import fold

_SOCIAL_HOSTS = (
    "instagram.com",
    "facebook.com",
    "fb.com",
    "tiktok.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
)

_WHATSAPP_RE = re.compile(r"wa\.me|api\.whatsapp|whatsapp\.com|whatsapp", re.I)
_FORM_RE = re.compile(r"<form\b|type=[\"']email[\"']|newsletter|assine", re.I)
_SHOP_RE = re.compile(
    r"carrinho|add-to-cart|woocommerce|nuvemshop|tray|loja\s+virtual|marketplace",
    re.I,
)
_OLD_RE = re.compile(
    r"copyright\s*(©|&copy;)?\s*(19\d{2}|200\d|201[0-6])|flash|guestbook|tabela\s+layout",
    re.I,
)
_VIEWPORT_RE = re.compile(r"name=[\"']viewport[\"']", re.I)


@dataclass
class OpportunityReport:
    digital_presence: str
    opportunities: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    personalized_line: str = ""
    flags: list[str] = field(default_factory=list)

    def as_extra(self) -> dict[str, Any]:
        return {
            "digital_presence": self.digital_presence,
            "opportunities": self.opportunities,
            "services": self.services,
            "personalized_line": self.personalized_line,
            "flags": self.flags,
        }

    def crm_description(self, *, company: str, city: str, origin: str) -> str:
        ops = "; ".join(self.opportunities) or "presença digital / automação (geral)"
        svcs = "; ".join(self.services) or "site institucional, landing page, SEO"
        return (
            f"origem={origin}\n"
            f"empresa={company}\n"
            f"cidade={city}\n"
            f"presenca={self.digital_presence}\n"
            f"oportunidades={ops}\n"
            f"servicos={svcs}"
        )


def is_social_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS)


def analyze_opportunity(
    *,
    name: str,
    website: str = "",
    snippet: str = "",
    scrape: dict | None = None,
    extra: dict | None = None,
) -> OpportunityReport:
    extra = extra or {}
    scrape = scrape or extra.get("scrape") or {}
    blob = fold(f"{name} {snippet} {website} {scrape}")
    pages = " ".join(str(p) for p in (scrape.get("pages") or []))
    raw_sample = str(scrape.get("raw_text_sample") or extra.get("raw_text_sample") or "")
    htmlish = f"{pages} {raw_sample} {snippet}"

    flags: list[str] = []
    opportunities: list[str] = []
    services: list[str] = []

    site = (website or "").strip()
    social_only = bool(site) and is_social_url(site)
    no_site = not site or social_only

    if no_site:
        flags.append("sem_site")
        if social_only:
            flags.append("so_rede_social")
            opportunities.append("presença só em rede social, sem site institucional")
        else:
            opportunities.append("não foi encontrado site institucional")
        services.append("criação de site institucional")
        services.append("presença no Google / SEO local")
        if social_only:
            services.append("landing page / single page")
    else:
        flags.append("tem_site")
        if _OLD_RE.search(htmlish) or _OLD_RE.search(blob):
            flags.append("site_antigo")
            opportunities.append("site com sinais de desatualização")
            services.append("modernização / redesign do site")
            if "landing page / single page" not in services:
                services.append("landing page / single page")
        if raw_sample and not _VIEWPORT_RE.search(raw_sample):
            flags.append("sem_viewport")
            opportunities.append("site sem indício claro de versão móvel")
            services.append("otimização para dispositivos móveis")
        if not _WHATSAPP_RE.search(htmlish) and "whatsapp" not in blob:
            flags.append("sem_whatsapp")
            opportunities.append("sem canal WhatsApp visível")
            services.append("integração com WhatsApp")
        if not _FORM_RE.search(htmlish):
            flags.append("sem_formulario")
            opportunities.append("pouca captação de leads visível no site")
            services.append("formulários e captação de leads")
        social_hit = any(h in blob or h in fold(htmlish) for h in ("instagram", "facebook"))
        if not social_hit:
            flags.append("sem_redes")
            opportunities.append("pouca integração visível com redes sociais")
            services.append("integração com Instagram/Facebook")
        if _SHOP_RE.search(blob) or _SHOP_RE.search(htmlish):
            flags.append("vende_produto")
            opportunities.append("indício de venda de produtos / loja")
            services.insert(0, "integração site, catálogo e marketplaces")

    # atendimento / serviço local
    if any(
        k in blob
        for k in (
            "clinica",
            "clínica",
            "consultorio",
            "restaurante",
            "oficina",
            "imobiliaria",
            "salão",
            "salao",
            "hotel",
            "atendimento",
        )
    ):
        flags.append("atendimento")
        if "automação de atendimento" not in services:
            services.append("automação de atendimento")
        if "automação de atendimento" not in " ".join(opportunities):
            opportunities.append("negócio com atendimento ao público")

    # corta para 1–3 serviços
    seen: set[str] = set()
    uniq_services: list[str] = []
    for s in services:
        if s not in seen:
            seen.add(s)
            uniq_services.append(s)
    uniq_services = uniq_services[:3]
    opportunities = opportunities[:4]

    if no_site and social_only:
        presence = "redes sociais, sem site próprio identificado"
        line = "Presença nas redes, sem site institucional. Dá para criar essa base e aparecer melhor no Google."
    elif no_site:
        presence = "site institucional não identificado"
        line = "Não há site institucional visível. Um site ou landing page já organiza o contato e a busca no Google."
    elif "site_antigo" in flags or "sem_viewport" in flags:
        presence = "site próprio com sinais de desatualização"
        line = "O site já existe — dá para modernizar, deixar mobile e melhorar o posicionamento no Google."
    elif "vende_produto" in flags:
        presence = "site próprio com indício de venda/catálogo"
        line = "O site já vende. Uma landing objetiva e SEO local ajudam o cliente a chegar até lá."
    elif "atendimento" in flags:
        presence = "site próprio; negócio com atendimento ao público"
        line = "Negócio de atendimento: um site claro e bem posicionado no Google traz o cliente até o contato."
    elif uniq_services:
        presence = "site próprio identificado"
        line = "Presença online já existe. Dá para evoluir o site, uma landing ou o SEO."
    else:
        presence = "informação limitada sobre a presença digital"
        line = "Site institucional, landing page, template ou SEO — o essencial para o cliente te encontrar."
        if not uniq_services:
            uniq_services = [
                "criação de site institucional",
                "landing page / single page",
                "presença no Google / SEO local",
            ]

    if not opportunities:
        opportunities = ["oportunidade ampla em presença digital e automação"]

    return OpportunityReport(
        digital_presence=presence,
        opportunities=opportunities,
        services=uniq_services,
        personalized_line=line,
        flags=flags,
    )
