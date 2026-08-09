"""Enriquecimento de e-mail — prioriza e-mail do DOMÍNIO do site.

Fluxo multi-pass:
1. Já tem e-mail do domínio → ok
2. Scrape site (httpx) → filtra *@dominio
3. Scrape deep (Playwright) → filtra *@dominio
4. Busca web no domínio (DDG/Bing): site:dominio, "@dominio", contato@dominio
5. Sem e-mail do domínio → tenta free-mail só se allow_free_mail
6. Sem e-mail → descartar
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.providers.domain_email import (
    domain_search_queries,
    extract_registrable_domain,
    matches_company_domain,
    pick_best_email,
)
from app.providers.scraper import (
    EMAIL_RE,
    extract_emails,
    normalize_url,
    scrape_website,
)
from app.providers.search_free import ddg_search

logger = get_logger(__name__)


def has_valid_email(value: str | None) -> bool:
    if not value or "@" not in value:
        return False
    email = value.strip().lower()
    return bool(EMAIL_RE.fullmatch(email) or EMAIL_RE.search(email))


def normalize_email(value: str) -> str:
    m = EMAIL_RE.search(value or "")
    return (m.group(1) if m else value).strip().lower()


def company_domain_of(contact: ProviderResult) -> str:
    return extract_registrable_domain(contact.website or "")


async def find_email_multi_pass(
    contact: ProviderResult,
    *,
    deep: bool = True,
    require_domain: bool = True,
    allow_free_mail: bool = False,
) -> ProviderResult:
    """
    Busca e-mail preferindo o domínio do website.

    require_domain=True (padrão): só aceita e-mail *@dominio-da-empresa
    """
    domain = company_domain_of(contact)

    # 0) já tem e-mail
    if has_valid_email(contact.email):
        contact.email = normalize_email(contact.email)
        if require_domain and domain and not matches_company_domain(contact.email, domain):
            # e-mail genérico (gmail etc.) — continua buscando do domínio
            logger.info(
                "email_not_on_domain_continue",
                email=contact.email,
                domain=domain,
            )
        else:
            return contact

    def _accept(candidates: list[str], source: str) -> bool:
        best = pick_best_email(
            candidates,
            company_domain=domain,
            require_domain=require_domain and bool(domain),
        )
        if not best and allow_free_mail and not require_domain:
            best = pick_best_email(candidates, company_domain=domain, require_domain=False)
        if best:
            contact.email = best
            _merge_extra(contact, {"email_source": source, "email_domain": domain})
            logger.info(
                "email_found",
                email=best,
                source=source,
                domain=domain or None,
                company=contact.company_name,
            )
            return True
        return False

    # 1) snippet / extra
    blob = " ".join(
        str(x)
        for x in [
            (contact.extra or {}).get("snippet", ""),
            contact.company_name,
            contact.contact_name,
        ]
        if x
    )
    if _accept(extract_emails(blob, site_host=domain), "snippet"):
        return contact

    website = normalize_url(contact.website or "")
    if not website:
        if deep and contact.company_name:
            contact = await _search_domain_or_company(contact, domain, _accept)
        return contact

    from app.core.config import get_settings
    from app.core.limits import enrich_semaphore

    settings = get_settings()
    max_pages = max(2, min(settings.scrape_max_pages, 4))

    async with enrich_semaphore():
        # 2) scrape pass 1 (httpx only — barato)
        scrape1 = await scrape_website(
            website, use_playwright=False, max_pages=max_pages
        )
        _merge_scrape_extra(contact, scrape1, "pass1")
        if scrape1.phones and not contact.phone:
            contact.phone = scrape1.best_phone
        if _accept(scrape1.emails, "scrape_pass1"):
            return contact

        if not deep:
            return contact

        # 3) Playwright só se habilitado (default OFF — pesa CPU/RAM)
        use_pw = bool(settings.enrich_playwright and settings.scrape_use_playwright)
        if use_pw:
            logger.info(
                "email_pass2_start",
                company=contact.company_name,
                domain=domain,
                url=website,
            )
            scrape2 = await scrape_website(
                website, use_playwright=True, max_pages=min(max_pages + 1, 5)
            )
            _merge_scrape_extra(contact, scrape2, "pass2_playwright")
            if scrape2.phones and not contact.phone:
                contact.phone = scrape2.best_phone
            if _accept(scrape2.emails, "scrape_pass2"):
                return contact

        # 4) busca web focada no domínio (poucas queries)
        contact = await _search_domain_or_company(contact, domain, _accept)
        return contact


async def _search_domain_or_company(
    contact: ProviderResult,
    domain: str,
    accept_fn,
) -> ProviderResult:
    from app.core.config import get_settings

    settings = get_settings()
    max_q = max(1, int(settings.enrich_max_domain_queries))

    queries = domain_search_queries(domain, contact.company_name or "")
    if not queries and contact.company_name:
        queries = [f'"{contact.company_name}" email contato']

    all_emails: list[str] = []
    for q in queries[:max_q]:
        try:
            results = await ddg_search(q, num=8)
        except Exception as exc:
            logger.debug("domain_search_failed", query=q, error=str(exc))
            continue
        for item in results:
            blob = f"{item.get('title','')} {item.get('snippet','')} {item.get('link','')}"
            all_emails.extend(extract_emails(blob, site_host=domain))
            link = item.get("link") or ""
            if link and not contact.website and domain and domain in link:
                contact.website = link
        if accept_fn(all_emails, f"web:{q[:40]}"):
            return contact

    return contact


async def require_email(
    contact: ProviderResult,
    *,
    deep: bool = True,
    require_domain: bool = True,
) -> Optional[ProviderResult]:
    """
    Enriquecer e-mail do domínio. Sem e-mail válido → None (descartar).
    """
    domain = company_domain_of(contact)
    result = await find_email_multi_pass(
        contact,
        deep=deep,
        require_domain=require_domain and bool(domain),
        allow_free_mail=not domain,  # sem site/domínio, aceita o que achar
    )
    if not has_valid_email(result.email):
        logger.info(
            "lead_discarded_no_email",
            company=result.company_name,
            website=result.website or None,
            domain=domain or None,
        )
        return None

    result.email = normalize_email(result.email)

    # se exige domínio e não bate, descarta
    if require_domain and domain and not matches_company_domain(result.email, domain):
        logger.info(
            "lead_discarded_email_wrong_domain",
            email=result.email,
            domain=domain,
            company=result.company_name,
        )
        return None

    return result


def _merge_scrape_extra(contact: ProviderResult, scrape, pass_name: str) -> None:
    extra = dict(contact.extra or {})
    extra["scrape"] = {
        "pass": pass_name,
        "method": scrape.method,
        "pages": scrape.pages_visited,
        "emails_found": scrape.emails[:8],
    }
    contact.extra = extra


def _merge_extra(contact: ProviderResult, data: dict) -> None:
    extra = dict(contact.extra or {})
    extra.update(data)
    contact.extra = extra
