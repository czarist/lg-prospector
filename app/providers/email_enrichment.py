"""Enriquecimento de e-mail — prioriza e-mail do DOMÍNIO do site.

Fluxo multi-pass:
1. Já tem e-mail do domínio → ok
2. Scrape site (httpx) → filtra *@dominio
3. Scrape deep (Playwright) → filtra *@dominio
4. Busca web no domínio (mesmo backend do discover — Serper se houver key)
5. Sem e-mail do domínio → tenta free-mail só se allow_free_mail
6. Sem e-mail → descartar
"""

from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.providers.domain_email import (
    domain_search_queries,
    extract_registrable_domain,
    matches_company_domain,
    pick_best_email,
)
from app.providers.geo_email import classify_contact_email, email_needs_llm_review
from app.providers.scraper import (
    EMAIL_RE,
    extract_emails,
    normalize_url,
    scrape_website,
)
from app.providers.http_tools import web_search

logger = get_logger(__name__)


async def verify_email_deliverable(email: str) -> tuple[bool, str | None]:
    """Confirma (best-effort, sem enviar nada) que o domínio do e-mail existe e
    tem registro MX/A — pega os casos de "domínio inventado/não existe" antes
    do envio. Não garante que a caixa postal específica exista (isso exigiria
    handshake SMTP com RCPT TO, que a maioria dos provedores/redes bloqueia ou
    responde de forma não confiável para probes)."""
    try:
        await asyncio.to_thread(validate_email, email, check_deliverability=True)
        return True, None
    except EmailNotValidError as exc:
        return False, str(exc)
    except Exception as exc:  # DNS instável/timeout — não descarta por falha da checagem
        logger.debug("email_deliverability_check_error", email=email, error=str(exc))
        return True, None


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


def _party_of(contact: ProviderResult) -> str:
    extra = contact.extra or {}
    return str(extra.get("tse_partido") or extra.get("partido") or "")


def email_fits_contact(email: str, contact: ProviderResult) -> tuple[bool, str]:
    """Sintaxe + geo/nome: e-mail tem que parecer do lead, não de um SERP aleatório."""
    return classify_contact_email(
        email,
        name=contact.contact_name or contact.company_name or "",
        city=contact.city or "",
        party=_party_of(contact),
        website=contact.website or "",
        segment=contact.segment or "",
    )


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
        seed_ok, seed_reason = email_fits_contact(contact.email, contact)
        if not seed_ok:
            logger.info(
                "email_seed_implausible_continue",
                email=contact.email,
                reason=seed_reason,
                company=contact.company_name,
            )
            contact.email = ""
        elif require_domain and domain and not matches_company_domain(contact.email, domain):
            # e-mail genérico (gmail etc.) — continua buscando do domínio
            logger.info(
                "email_not_on_domain_continue",
                email=contact.email,
                domain=domain,
            )
        else:
            return contact

    def _accept(candidates: list[str], source: str) -> bool:
        plausible = [c for c in candidates if email_fits_contact(c, contact)[0]]
        best = pick_best_email(
            plausible,
            company_domain=domain,
            require_domain=require_domain and bool(domain),
        )
        if not best and allow_free_mail and not require_domain:
            best = pick_best_email(plausible, company_domain=domain, require_domain=False)
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
            results = await web_search(
                q,
                num=8,
                city=contact.city or "",
                state=contact.state or "",
            )
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
    allow_free_mail: bool | None = None,
) -> Optional[ProviderResult]:
    """
    Enriquecer e-mail do domínio. Sem e-mail válido → None (descartar).

    allow_free_mail=None → só se não houver domínio de site (padrão).
    Para político/campanha passe True (equipes usam gmail etc.).
    """
    domain = company_domain_of(contact)
    if allow_free_mail is None:
        allow_free_mail = not domain
    # se já tem e-mail e free-mail liberado, não força domínio
    req_dom = require_domain and bool(domain) and not allow_free_mail
    result = await find_email_multi_pass(
        contact,
        deep=deep,
        require_domain=req_dom,
        allow_free_mail=allow_free_mail,
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

    fit_ok, fit_reason = email_fits_contact(result.email, result)
    if not fit_ok:
        logger.info(
            "lead_discarded_email_implausible",
            email=result.email,
            company=result.company_name,
            reason=fit_reason,
        )
        return None

    # se exige domínio e não bate, descarta (exceto free-mail liberado)
    if (
        require_domain
        and domain
        and not allow_free_mail
        and not matches_company_domain(result.email, domain)
    ):
        logger.info(
            "lead_discarded_email_wrong_domain",
            email=result.email,
            domain=domain,
            company=result.company_name,
        )
        return None

    # nunca aceitar e-mail de órgão público / .gov / .leg / .jus
    from app.providers.public_org import is_public_email, is_public_organ

    allow_gov = (result.segment or "").lower() == "generalista"
    if is_public_email(result.email, allow_gov_br=allow_gov):
        logger.info(
            "lead_discarded_public_email",
            email=result.email,
            company=result.company_name,
        )
        return None
    if is_public_organ(
        name=result.company_name or "",
        website=result.website or "",
        email=result.email or "",
        segment=result.segment or "",
        allow_gov_br=allow_gov,
    ):
        logger.info(
            "lead_discarded_public_organ",
            email=result.email,
            company=result.company_name,
            website=result.website,
        )
        return None

    from app.core.config import get_settings

    if get_settings().enrich_verify_email_dns:
        deliverable, reason = await verify_email_deliverable(result.email)
        if not deliverable:
            logger.info(
                "lead_discarded_email_undeliverable",
                email=result.email,
                company=result.company_name,
                reason=reason,
            )
            return None

    if email_needs_llm_review(result.email):
        from app.infrastructure.llm.client import score_email_belongs_to_business

        snippet = str((result.extra or {}).get("snippet") or "")
        verdict = await score_email_belongs_to_business(
            email=result.email,
            name=result.contact_name or result.company_name or "",
            website=result.website or "",
            city=result.city or "",
            segment=result.segment or "",
            snippet=snippet,
        )
        _merge_extra(result, {"llm_email": verdict})
        if not verdict.get("keep", True):
            logger.info(
                "lead_discarded_email_llm",
                email=result.email,
                company=result.company_name,
                reason=verdict.get("reason"),
                score=verdict.get("score"),
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
