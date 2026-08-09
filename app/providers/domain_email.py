"""Utilitários de domínio e e-mail corporativo.

Prioriza e-mails do mesmo domínio do site da empresa
(ex.: site acme.com.br → contato@acme.com.br).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.providers.scraper import EMAIL_RE, extract_emails, is_valid_email, score_email

# TLDs compostos comuns BR
_MULTI_TLDS = (
    "com.br",
    "org.br",
    "net.br",
    "gov.br",
    "edu.br",
    "adv.br",
    "eng.br",
    "leg.br",
    "co.uk",
)

# Locais preferidos em e-mail comercial BR
PREFERRED_LOCALS = (
    "contato",
    "contact",
    "comercial",
    "vendas",
    "sales",
    "hello",
    "ola",
    "info",
    "adm",
    "admin",
    "atendimento",
    "escritorio",
    "recepcao",
    "secretaria",
)

FREE_MAIL = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "yahoo.com",
    "yahoo.com.br",
    "icloud.com",
    "uol.com.br",
    "bol.com.br",
    "terra.com.br",
    "ig.com.br",
    "proton.me",
    "protonmail.com",
}


def extract_registrable_domain(url_or_host: str) -> str:
    """
    Extrai domínio registrável a partir de URL ou host.
    https://www.blog.acme.com.br/x → acme.com.br
    """
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc or urlparse(raw).path
    host = host.split("@")[-1].split(":")[0].strip().removeprefix("www.")
    if not host or "." not in host:
        return host

    for tld in _MULTI_TLDS:
        if host.endswith("." + tld) or host == tld:
            parts = host[: -len(tld)].rstrip(".").split(".")
            base = parts[-1] if parts and parts[-1] else ""
            return f"{base}.{tld}" if base else host

    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].removeprefix("www.")


def is_free_mail(email: str) -> bool:
    return email_domain(email) in FREE_MAIL


def matches_company_domain(email: str, company_domain: str) -> bool:
    """True se o e-mail é do domínio da empresa (ou subdomínio)."""
    ed = email_domain(email)
    cd = extract_registrable_domain(company_domain) if company_domain else ""
    if not ed or not cd:
        return False
    return ed == cd or ed.endswith("." + cd)


def pick_best_email(
    candidates: list[str],
    *,
    company_domain: str = "",
    require_domain: bool = False,
) -> str:
    """
    Escolhe o melhor e-mail.
    Se require_domain=True, só aceita e-mail do domínio da empresa.
    """
    domain = extract_registrable_domain(company_domain) if company_domain else ""
    cleaned: list[str] = []
    for raw in candidates:
        for m in EMAIL_RE.findall(raw or ""):
            e = m.strip().lower().removeprefix("mailto:")
            if is_valid_email(e):
                cleaned.append(e)
        e2 = (raw or "").strip().lower()
        if "@" in e2 and is_valid_email(e2):
            cleaned.append(e2)

    # unique preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for e in cleaned:
        if e not in seen:
            seen.add(e)
            uniq.append(e)

    if not uniq:
        return ""

    def _rank(e: str) -> tuple:
        on_domain = 1 if matches_company_domain(e, domain) else 0
        free = 0 if is_free_mail(e) else 1
        local = e.split("@", 1)[0]
        pref = 0
        for i, p in enumerate(PREFERRED_LOCALS):
            if local == p or local.startswith(p):
                pref = 100 - i
                break
        sc = score_email(e, domain)
        return (on_domain, free, pref, sc)

    ranked = sorted(uniq, key=_rank, reverse=True)
    best = ranked[0]
    if require_domain and domain and not matches_company_domain(best, domain):
        domain_only = [e for e in ranked if matches_company_domain(e, domain)]
        return domain_only[0] if domain_only else ""
    # se há opção no domínio, prefere sempre
    if domain:
        domain_only = [e for e in ranked if matches_company_domain(e, domain)]
        if domain_only:
            return domain_only[0]
    return best


def candidate_locals_for_domain(domain: str) -> list[str]:
    """Gera candidatos contato@dominio, comercial@dominio, … (para busca)."""
    domain = extract_registrable_domain(domain)
    if not domain or domain in FREE_MAIL:
        return []
    return [f"{local}@{domain}" for local in PREFERRED_LOCALS[:8]]


def domain_search_queries(domain: str, company_name: str = "") -> list[str]:
    """Queries focadas em achar e-mail no domínio."""
    domain = extract_registrable_domain(domain)
    if not domain:
        return []
    qs = [
        f'"{domain}" email OR contato OR mailto',
        f"site:{domain} contato email",
        f"site:{domain} @ {domain.split('.')[0]}",
        f'"@{domain}"',
        f"contato@{domain}",
        f"comercial@{domain}",
        f"email@{domain}",
    ]
    if company_name:
        qs.insert(0, f'"{company_name}" "@{domain}"')
        qs.append(f'"{company_name}" contato@{domain}')
    return qs
