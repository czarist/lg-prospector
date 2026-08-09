"""Scraping local de sites — substituto self-hosted do Firecrawl.

Pipeline:
1. httpx GET (HTML estático)
2. Segue páginas de contato (/contato, /contact, mailto, etc.)
3. Playwright (opcional) se a página for SPA / sem e-mail
4. Extrai e-mails e telefones com filtros anti-spam

Sem custo de API. Limite = rate limit local + fair-use.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

EMAIL_RE = re.compile(
    r"(?:mailto:)?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)
PHONE_RE = re.compile(
    r"(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9?\d{4}[-\s.]?\d{4})"
)

# E-mails de tracking/CDN/lixo
EMAIL_BLOCKLIST_SUBSTR = (
    "example.com",
    "example.org",
    "sentry.io",
    "wixpress.com",
    "sentry-next",
    "schema.org",
    "w3.org",
    "googleapis.com",
    "gstatic.com",
    "cloudflare.com",
    "github.com",
    "placeholder",
    "domain.com",
    "email.com",
    "yourdomain",
    "seuemail",
    "seudominio",
    "test@",
    "noreply@",
    "no-reply@",
    "donotreply@",
    "mailer-daemon",
    "webpack",
    "localhost",
    ".png",
    ".jpg",
    ".gif",
    ".svg",
    ".css",
    ".js",
)

CONTACT_PATH_HINTS = (
    "contato",
    "contact",
    "fale-conosco",
    "fale_conosco",
    "about",
    "sobre",
    "equipe",
    "team",
    "quem-somos",
    "imprensa",
    "press",
    "comercial",
    "vendas",
    "sales",
    "orcamento",
    "orçamento",
    "atendimento",
    "trabalhe-conosco",  # às vezes tem e-mail RH; baixa prioridade
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LG-Prospector/0.1; +https://local) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass
class ScrapeResult:
    url: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    pages_visited: list[str] = field(default_factory=list)
    method: str = ""  # httpx | playwright | mixed
    raw_text_sample: str = ""
    error: str | None = None

    @property
    def best_email(self) -> str:
        return self.emails[0] if self.emails else ""

    @property
    def best_phone(self) -> str:
        return self.phones[0] if self.phones else ""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _same_host(base: str, candidate: str) -> bool:
    try:
        b = urlparse(base)
        c = urlparse(candidate)
        return (c.netloc or "").lower().removeprefix("www.") == (
            b.netloc or ""
        ).lower().removeprefix("www.")
    except Exception:
        return False


def is_valid_email(email: str) -> bool:
    email = email.strip().lower()
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if any(x in email for x in EMAIL_BLOCKLIST_SUBSTR):
        return False
    # Heurística: imagem ofuscada "nome [at] dom"
    if " " in email:
        return False
    return True


def score_email(email: str, site_host: str = "") -> int:
    """Maior score = melhor e-mail corporativo."""
    email = email.lower()
    score = 0
    local, _, domain = email.partition("@")
    host = site_host.lower().removeprefix("www.")
    if host and (domain in host or host in domain or domain.split(".")[0] in host):
        score += 50  # mesmo domínio do site
    for pref in ("contato", "contact", "comercial", "vendas", "sales", "info", "ola", "hello", "adm", "admin"):
        if local.startswith(pref) or local == pref:
            score += 20
            break
    for bad in ("noreply", "no-reply", "donotreply", "bounce", "mailer"):
        if bad in local:
            score -= 40
    if local in {"rh", "vagas", "jobs", "careers", "suporte", "support"}:
        score -= 5
    return score


def extract_emails(text: str, site_host: str = "") -> list[str]:
    found = EMAIL_RE.findall(text or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in found:
        email = raw.strip().lower().removeprefix("mailto:")
        # Decodifica entidades comuns
        email = email.replace("%40", "@")
        if not is_valid_email(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        cleaned.append(email)
    cleaned.sort(key=lambda e: score_email(e, site_host), reverse=True)
    return cleaned


def extract_phones(text: str) -> list[str]:
    found = PHONE_RE.findall(text or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for p in found:
        p = re.sub(r"\s+", " ", p.strip())
        digits = re.sub(r"\D", "", p)
        if len(digits) < 10 or len(digits) > 13:
            continue
        # descarta sequências repetidas / placeholder
        if len(set(digits[-8:])) <= 1:
            continue
        if p in seen or digits in seen:
            continue
        seen.add(p)
        seen.add(digits)
        cleaned.append(p)
    return cleaned


def extract_contact_links(html: str, base_url: str, max_links: int = 8) -> list[str]:
    """Descobre URLs internas de contato a partir do HTML."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "tel:")):
            continue
        if href.lower().startswith("mailto:"):
            continue  # e-mail já capturado no texto
        full = urljoin(base_url + "/", href)
        if not _same_host(base_url, full):
            continue
        path = urlparse(full).path.lower()
        text = (a.get_text(" ") or "").lower()
        blob = f"{path} {text} {href.lower()}"
        if any(h in blob for h in CONTACT_PATH_HINTS):
            # remove fragment/query ruído
            clean = full.split("#")[0].split("?")[0].rstrip("/")
            if clean not in seen and clean != base_url.rstrip("/"):
                seen.add(clean)
                links.append(clean)
        if len(links) >= max_links:
            break
    return links


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # Mantém também hrefs mailto e raw HTML para regex pegar e-mails ofuscados em attrs
    attrs_blob = " ".join(
        str(v) for tag in soup.find_all(True) for v in (tag.attrs or {}).values() if isinstance(v, str)
    )
    return f"{text}\n{attrs_blob}\n{html}"


async def fetch_httpx(url: str, timeout: float = 20.0) -> Optional[str]:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
            verify=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                logger.debug("scrape_http_status", url=url, status=resp.status_code)
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype and "xml" not in ctype:
                # ainda tenta se body parecer HTML
                if not resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
                    return None
            return resp.text
    except Exception as exc:
        logger.debug("scrape_httpx_failed", url=url, error=str(exc))
        return None


async def fetch_playwright(url: str, timeout_ms: int = 25000) -> Optional[str]:
    """Renderiza página com Playwright (sites JS). Serializado (1 browser)."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        logger.warning("playwright_not_installed")
        return None

    from app.core.limits import playwright_semaphore

    async with playwright_semaphore():
        return await _fetch_playwright_inner(url, timeout_ms)


async def _fetch_playwright_inner(url: str, timeout_ms: int = 25000) -> Optional[str]:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=DEFAULT_HEADERS["User-Agent"])
                page.set_default_timeout(timeout_ms)
                await page.goto(url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                return await page.content()
            finally:
                await browser.close()
    except Exception as exc:
        logger.debug("scrape_playwright_failed", url=url, error=str(exc))
        return None


async def scrape_website(
    url: str,
    *,
    max_pages: int | None = None,
    use_playwright: bool | None = None,
    timeout: float = 20.0,
) -> ScrapeResult:
    """
    Scrape multi-página focado em achar e-mail/telefone.

    Ordem:
    - home via httpx
    - páginas /contato etc. via httpx
    - se ainda sem e-mail e Playwright habilitado → home (+ 1 contato) via browser
    """
    settings = get_settings()
    max_pages = max_pages if max_pages is not None else getattr(settings, "scrape_max_pages", 4)
    if use_playwright is None:
        use_playwright = getattr(settings, "scrape_use_playwright", True)

    base = normalize_url(url)
    result = ScrapeResult(url=base)
    if not base:
        result.error = "empty_url"
        return result

    host = urlparse(base).netloc
    methods: set[str] = set()
    all_emails: list[str] = []
    all_phones: list[str] = []
    visited: list[str] = []

    async def process_html(page_url: str, html: str, method: str) -> None:
        methods.add(method)
        visited.append(page_url)
        text = html_to_text(html)
        all_emails.extend(extract_emails(text, site_host=host))
        all_phones.extend(extract_phones(text))
        if not result.raw_text_sample:
            result.raw_text_sample = text[:500]

    # 1) Home httpx
    html = await fetch_httpx(base, timeout=timeout)
    if html:
        await process_html(base, html, "httpx")
        contact_links = extract_contact_links(html, base, max_links=max_pages)
    else:
        contact_links = [
            urljoin(base + "/", path)
            for path in ("/contato", "/contact", "/fale-conosco", "/sobre", "/about")
        ]

    # 2) Páginas de contato httpx
    for link in contact_links[: max(0, max_pages - 1)]:
        if link in visited:
            continue
        page_html = await fetch_httpx(link, timeout=timeout)
        if page_html:
            await process_html(link, page_html, "httpx")
        # se já achou e-mail bom, para cedo
        ranked = extract_emails(" ".join(all_emails), site_host=host)
        if ranked and score_email(ranked[0], host) >= 40:
            break
        await asyncio.sleep(0.15)  # gentileza

    # 3) Playwright fallback
    ranked = extract_emails(" ".join(all_emails), site_host=host)
    if use_playwright and not ranked:
        logger.info("scrape_playwright_fallback", url=base)
        pw_html = await fetch_playwright(base)
        if pw_html:
            await process_html(base, pw_html, "playwright")
            # tenta 1 página de contato renderizada
            pw_links = extract_contact_links(pw_html, base, max_links=2)
            for link in pw_links[:1]:
                if link in visited:
                    continue
                pw2 = await fetch_playwright(link)
                if pw2:
                    await process_html(link, pw2, "playwright")

    # Dedup final
    result.emails = extract_emails(" ".join(all_emails), site_host=host)
    # phones: dedup preservando ordem
    seen_p: set[str] = set()
    phones: list[str] = []
    for p in all_phones:
        if p not in seen_p:
            seen_p.add(p)
            phones.append(p)
    result.phones = phones
    result.pages_visited = visited
    if len(methods) > 1:
        result.method = "mixed"
    elif methods:
        result.method = next(iter(methods))
    else:
        result.method = "none"
        result.error = "no_content"

    logger.info(
        "scrape_done",
        url=base,
        emails=len(result.emails),
        phones=len(result.phones),
        pages=len(visited),
        method=result.method,
        best_email=result.best_email or None,
    )
    return result


# Alias compatível com a antiga firecrawl_scrape
async def scrape_page_text(url: str) -> Optional[str]:
    """Retorna texto combinado das páginas visitadas (compat)."""
    result = await scrape_website(url)
    if not result.pages_visited and result.error:
        return None
    parts = [result.raw_text_sample]
    if result.emails:
        parts.append(" ".join(result.emails))
    if result.phones:
        parts.append(" ".join(result.phones))
    return "\n".join(parts) if any(parts) else None
