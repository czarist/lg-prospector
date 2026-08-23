"""Revalida leads já cadastrados com modelos remotos + IMAP de bounce.

Não usa Qwen local. Não apaga contato: tenta achar e-mail melhor se a
caixa atual não existe, enriquece lacunas com evidência do site/busca,
e marca a revisão para não repetir sem motivo.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.cities import all_city_targets
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.models import (
    CampaignItem,
    Company,
    Contact,
    EmailRecord,
    ItemStatus,
)
from app.infrastructure.email.bounce import CLASS_MAILBOX_MISSING
from app.infrastructure.llm.client import salvage_saved_contact
from app.providers.domain_email import (
    email_domain,
    extract_registrable_domain,
    is_free_mail,
    matches_company_domain,
    pick_best_email,
)
from app.providers.geo_email import (
    brand_domains_related,
    is_directory_host,
    is_foreign_company,
    is_junk_lead_name,
)
from app.providers.email_enrichment import (
    email_fits_contact,
    has_valid_email,
    normalize_email,
)
from app.providers.http_tools import web_search
from app.providers.scraper import extract_emails, extract_phones, scrape_website
from app.domain.entities.provider_result import ProviderResult
from app.services.generalist_service import mark_contact_mail_skip

logger = get_logger(__name__)

_INVALID_EMAIL_MARKERS = (
    "e-mail inválid",
    "email inválid",
    "e-mail invalido",
    "email invalido",
    "e-mail bounceado",
    "email bounceado",
    "caixa não existe",
    "caixa nao existe",
    "deve ser substituído",
    "deve ser substituido",
)

_BR_UFS = frozenset(
    {
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    }
)
_CITY_INDEX = {
    unicodedata.normalize("NFKD", t.city)
    .encode("ascii", "ignore")
    .decode()
    .lower(): t
    for t in all_city_targets()
}
_JUNK_SITE_MARKERS = (
    "wikipedia.org",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "linkedin.com",
    "jusbrasil.com.br",
    "escavador.com",
    "econodata.com.br",
    "cnpj.biz",
    "cnpja.com",
    "glassdoor.com",
    "indeed.com",
    "infojobs.com.br",
    "catho.com.br",
    "globo.com",
    "uol.com.br",
    "terra.com.br",
    "r7.com",
    "ig.com.br",
    "yahoo.com",
    "msn.com",
)
_GENERIC_CONTACT = frozenset(
    {"contato", "contact", "empresa", "comercial", "atendimento", "sac"}
)
_DIRTY_NAME_RE = re.compile(
    r"(https?://|\bcep\b|\d{5}-?\d{3}|wikipedia|linkedin\.com)", re.I
)
_GENERIC_LEAD_RE = re.compile(
    r"^(ag[eê]ncia|escrit[oó]rio|empresa|consultoria|desenvolvimento|"
    r"servi[cç]os?|marketing digital|links para)\b",
    re.I,
)
_LINKEDIN_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?linkedin\.com/(?:in|company|school)/[^\s/?#]+",
    re.I,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email_belongs_to_firm(em: str, website: str, current: str) -> bool:
    """Não troca para o domínio de OUTRA empresa."""
    site = extract_registrable_domain(website or "")
    if site and not is_directory_host(site) and not is_directory_host(website or ""):
        if matches_company_domain(em, site):
            return True
        return brand_domains_related(site, extract_registrable_domain(email_domain(em)))
    cur_dom = email_domain(current)
    if cur_dom and not is_free_mail(current) and not is_directory_host(cur_dom):
        cand = extract_registrable_domain(email_domain(em))
        cur = extract_registrable_domain(cur_dom)
        return bool(cand and cur and (cand == cur or matches_company_domain(em, cur_dom)))
    return False


def _verdict_wants_replace(verdict: dict[str, Any]) -> bool:
    action = str(verdict.get("email_action") or "keep").strip().lower()
    if action == "replace":
        return True
    blob = f"{verdict.get('reason') or ''} {verdict.get('analysis') or ''}".lower()
    return any(m in blob for m in _INVALID_EMAIL_MARKERS)


def _fold(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text or "")
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )


def _blank(value: str | None) -> bool:
    return not (value or "").strip()


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _looks_dirty_name(name: str) -> bool:
    n = (name or "").strip()
    if not n or len(n) > 90:
        return True
    if _DIRTY_NAME_RE.search(n):
        return True
    if n.count("|") >= 1 and len(n) > 40:
        return True
    low = n.lower()
    # título de página/SERP no lugar do nome fantasia
    if " em " in low and len(n) >= 28:
        return True
    if low.startswith(("o que é", "como ", "melhores ", "lista de")):
        return True
    # "Desenvolvimento de Software Fortaleza" — categoria+cidade, sem marca
    if _GENERIC_LEAD_RE.match(n) and len(n) >= 32:
        return True
    return False


def _generic_person(name: str, company_name: str) -> bool:
    n = (name or "").strip().lower()
    if not n or n in _GENERIC_CONTACT:
        return True
    cn = (company_name or "").strip().lower()
    return bool(cn) and n == cn


def _in_evidence(value: str, blob: str, *, phone: bool = False) -> bool:
    if not value or not blob:
        return False
    if phone:
        d = _digits(value)
        return len(d) >= 10 and d in _digits(blob)
    needle = _fold(value)
    if len(needle) < 3:
        return False
    hay = _fold(blob)
    if needle in hay:
        return True
    parts = [p for p in re.split(r"[\s\-/]+", needle) if len(p) >= 4]
    return bool(parts) and all(p in hay for p in parts[:4])


def _normalize_website(raw: str) -> str:
    url = (raw or "").strip()
    if not url or " " in url:
        return ""
    if not url.startswith(("http://", "https://")):
        if "." not in url:
            return ""
        url = "https://" + url
    host = (urlparse(url).netloc or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return ""
    if any(m in host for m in _JUNK_SITE_MARKERS):
        return ""
    return url.split("#")[0].split("?")[0][:512]


def _normalize_linkedin(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    m = _LINKEDIN_RE.search(text)
    if m:
        return m.group(0)[:512]
    if "linkedin.com/" in text.lower():
        if not text.startswith("http"):
            text = "https://" + text.lstrip("/")
        return text.split("?")[0][:512]
    return ""


def _normalize_phone(raw: str) -> str:
    found = extract_phones(raw or "")
    if found:
        return found[0][:64]
    digits = _digits(raw)
    if 10 <= len(digits) <= 13:
        return (raw or "").strip()[:64]
    return ""


def _normalize_uf(raw: str) -> str:
    uf = (raw or "").strip().upper()
    if len(uf) > 2:
        uf = uf[:2]
    return uf if uf in _BR_UFS else ""


def _auditor_blob(extra: dict[str, Any] | None) -> dict[str, Any]:
    extra = extra or {}
    blob = extra.get("auditor")
    return dict(blob) if isinstance(blob, dict) else {}


def needs_audit(contact: Contact) -> bool:
    extra = contact.extra or {}
    reviewed = _auditor_blob(extra).get("reviewed_at")
    bounce = extra.get("email_bounce") if isinstance(extra.get("email_bounce"), dict) else {}
    mailbox_gone = (bounce.get("classification") or "") == CLASS_MAILBOX_MISSING
    recovered = bool(_auditor_blob(extra).get("recovered_email"))
    if mailbox_gone and not recovered:
        return True
    return not reviewed


class AuditorService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._bounce_by_addr: dict[str, dict[str, Any]] = {}

    async def load_bounce_index(self, *, imap: bool = True) -> int:
        """DB + IMAP (lido uma vez). Chave = e-mail destinatário."""
        self._bounce_by_addr = {}
        rows = (
            await self.session.execute(
                select(EmailRecord).where(EmailRecord.status == "bounced")
            )
        ).scalars().all()
        for rec in rows:
            addr = (rec.to_address or "").strip().lower()
            if not addr:
                continue
            self._bounce_by_addr[addr] = {
                "classification": "mailbox_missing"
                if rec.error_message and "mailbox_missing" in (rec.error_message or "")
                else "unknown",
                "diagnostic": (rec.error_message or "")[:300],
                "source": "db",
            }
            if "mailbox_missing" in (rec.error_message or ""):
                self._bounce_by_addr[addr]["classification"] = CLASS_MAILBOX_MISSING

        if imap:
            try:
                n = await self._merge_imap_bounces()
                logger.info("auditor_imap_merged", extra=n)
            except Exception as exc:
                logger.warning("auditor_imap_skip", error=str(exc)[:160])
        return len(self._bounce_by_addr)

    async def _merge_imap_bounces(self) -> int:
        from app.core.config import get_settings
        from app.infrastructure.email.bounce import parse_bounce_message
        from app.infrastructure.email.imap_client import ImapMailbox

        settings = get_settings()
        since = max(1, int(settings.auditor_imap_since_days))

        def _scan() -> list[tuple[str, str, str]]:
            out: list[tuple[str, str, str]] = []
            with ImapMailbox() as box:
                for msg in box.iter_candidates(since_days=since, limit=120):
                    hit = parse_bounce_message(msg.raw, imap_uid=msg.uid)
                    if not hit.is_bounce and not hit.recipients:
                        continue
                    for addr in hit.recipients:
                        out.append(
                            (
                                addr.strip().lower(),
                                hit.classification or "unknown",
                                (hit.diagnostic or hit.subject or "")[:300],
                            )
                        )
            return out

        import asyncio

        rows = await asyncio.to_thread(_scan)
        added = 0
        for addr, classif, diag in rows:
            if not addr or "@" not in addr:
                continue
            prev = self._bounce_by_addr.get(addr)
            if not prev or classif == CLASS_MAILBOX_MISSING:
                self._bounce_by_addr[addr] = {
                    "classification": classif,
                    "diagnostic": diag,
                    "source": "imap",
                }
                added += 1
        return added

    def _pending_clause(self):
        """Ainda sem auditor.reviewed_at — não limitar aos 8 mais antigos."""
        reviewed_at = func.json_extract(Contact.extra, "$.auditor.reviewed_at")
        return or_(Contact.extra.is_(None), reviewed_at.is_(None))

    async def count_pending(self) -> int:
        n = (
            await self.session.execute(
                select(func.count())
                .select_from(Contact)
                .where(
                    Contact.email.is_not(None),
                    Contact.email != "",
                    self._pending_clause(),
                )
            )
        ).scalar()
        return int(n or 0)

    async def reset_reviews(self) -> dict[str, int]:
        """Apaga extra.auditor — próxima subida reavalia todo mundo.

        Não mexe em e-mail, telefone, bounce nem no que já foi enriquecido
        nas colunas. Só a marca de 'já revisado'.
        """
        marked = (
            await self.session.execute(
                select(func.count())
                .select_from(Contact)
                .where(func.json_extract(Contact.extra, "$.auditor.reviewed_at").is_not(None))
            )
        ).scalar()
        result = await self.session.execute(
            text(
                "UPDATE contacts "
                "SET extra = JSON_REMOVE(extra, '$.auditor') "
                "WHERE extra IS NOT NULL "
                "AND JSON_EXTRACT(extra, '$.auditor') IS NOT NULL"
            )
        )
        await self.session.commit()
        pending = await self.count_pending()
        return {
            "cleared": int(result.rowcount if result.rowcount is not None and result.rowcount >= 0 else marked or 0),
            "had_reviewed_at": int(marked or 0),
            "pending": pending,
        }

    async def next_contacts(
        self, *, limit: int = 1, exclude_ids: set[str] | None = None
    ) -> list[Contact]:
        want = max(1, int(limit))
        skip = {x for x in (exclude_ids or set()) if x}
        clauses = [
            Contact.email.is_not(None),
            Contact.email != "",
            self._pending_clause(),
        ]
        if skip:
            clauses.append(Contact.id.notin_(skip))
        rows = (
            await self.session.execute(
                select(Contact)
                .where(*clauses)
                .options(selectinload(Contact.company), selectinload(Contact.emails))
                .order_by(Contact.created_at.asc())
                .limit(want)
            )
        ).scalars().all()
        return [ct for ct in rows if needs_audit(ct) and ct.id not in skip][:want]

    @staticmethod
    def _col_blank_or_eq(column, value: str | None):
        v = (value or "").strip()
        if not v:
            return or_(column.is_(None), column == "")
        return column == v

    async def _company_key_taken(
        self,
        *,
        name: str,
        city: str | None,
        website_host: str | None,
        exclude_id: str,
    ) -> bool:
        """True se outra empresa já ocupa o unique name+city+host."""
        name_v = (name or "").strip()[:191]
        if not name_v or not exclude_id:
            return False
        hit = (
            await self.session.execute(
                select(Company.id)
                .where(
                    Company.name == name_v,
                    Company.id != exclude_id,
                    self._col_blank_or_eq(Company.city, city),
                    self._col_blank_or_eq(Company.website_host, website_host),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return hit is not None

    def _known_gaps(self, contact: Contact, company: Company | None) -> list[str]:
        gaps: list[str] = []
        cname = (company.name if company else "") or ""
        if company is None or _looks_dirty_name(company.name or ""):
            gaps.append("clean_name")
        if company is None or _blank(company.website):
            gaps.append("website")
        if company is None or _blank(company.city):
            gaps.append("city")
        if company is None or _blank(company.state):
            gaps.append("state")
        if _blank(contact.phone) and (company is None or _blank(company.phone)):
            gaps.append("phone")
        if _generic_person(contact.name or "", cname):
            gaps.append("contact_name")
        if _blank(contact.role):
            gaps.append("role")
        if _blank(contact.linkedin):
            gaps.append("linkedin")
        return gaps

    async def gather_candidates(
        self, contact: Contact
    ) -> tuple[list[str], str, dict[str, Any]]:
        company = contact.company
        name = (company.name if company else "") or contact.name or ""
        website = (company.website if company else "") or ""
        city = (company.city if company else "") or ""
        state = (company.state if company else "") or ""
        found: list[str] = []
        phones: list[str] = []
        sites: list[str] = []
        linkedin: list[str] = []
        notes: list[str] = []
        settings = get_settings()
        scrape_pages = max(2, int(getattr(settings, "auditor_scrape_pages", 3)))

        if website:
            try:
                scrape = await scrape_website(
                    website, use_playwright=False, max_pages=scrape_pages
                )
                found.extend(scrape.emails[:12])
                phones.extend(scrape.phones[:8])
                if scrape.br_signals:
                    notes.append("site_br=" + ",".join(scrape.br_signals[:6]))
                if scrape.foreign_signals:
                    notes.append(
                        "site_estrangeiro=" + ",".join(scrape.foreign_signals[:4])
                    )
                sample = (scrape.raw_text_sample or "").strip()
                if sample:
                    notes.append("amostra_site=" + sample[:900])
                    found.extend(extract_emails(sample))
                    phones.extend(extract_phones(sample))
                    linkedin.extend(_LINKEDIN_RE.findall(sample))
            except Exception as exc:
                notes.append(f"scrape_erro={type(exc).__name__}")

        n_hits = max(4, int(settings.auditor_search_hits))
        queries = [
            f'"{name}" {city} email contato telefone'.strip(),
        ]
        if _blank(website):
            queries.append(f'"{name}" {city} site oficial'.strip())
        if _blank(contact.linkedin):
            queries.append(f'"{name}" {city} linkedin'.strip())

        seen_q: set[str] = set()
        for q in queries:
            key = q.lower()
            if key in seen_q:
                continue
            seen_q.add(key)
            try:
                hits = await web_search(q, num=n_hits, city=city, state=state)
            except Exception as exc:
                notes.append(f"busca_erro={type(exc).__name__}")
                continue
            for item in hits[:8]:
                title = (item.get("title") or "").strip()
                snippet = (item.get("snippet") or item.get("description") or "").strip()
                link = (item.get("link") or item.get("url") or "").strip()
                blob = f"{title} {snippet} {link}"
                found.extend(extract_emails(blob))
                phones.extend(extract_phones(blob))
                linkedin.extend(_LINKEDIN_RE.findall(blob))
                ln = _normalize_linkedin(link)
                if ln:
                    linkedin.append(ln)
                site = _normalize_website(link)
                if site:
                    sites.append(site)
                bit = " — ".join(p for p in (title[:90], snippet[:180], link[:120]) if p)
                if bit:
                    notes.append(bit)

        def _uniq(seq: list[str], *, limit: int) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for raw in seq:
                v = (raw or "").strip()
                k = v.lower()
                if not v or k in seen:
                    continue
                seen.add(k)
                out.append(v)
                if len(out) >= limit:
                    break
            return out

        phones = _uniq(phones, limit=8)
        sites = _uniq(sites, limit=6)
        linkedin = _uniq(linkedin, limit=4)
        current = (contact.email or "").strip().lower()
        emails: list[str] = []
        seen_em: set[str] = set()
        for raw in found:
            em = normalize_email(raw)
            if not has_valid_email(em) or em in seen_em or em == current:
                continue
            seen_em.add(em)
            emails.append(em)
            if len(emails) >= 10:
                break

        gaps = self._known_gaps(contact, company)
        header = [
            f"lacunas={','.join(gaps) or 'nenhuma'}",
            f"telefones_encontrados={', '.join(phones) or 'nenhum'}",
            f"sites_encontrados={', '.join(sites) or 'nenhum'}",
            f"linkedin_encontrado={', '.join(linkedin) or 'nenhum'}",
        ]
        dossier = "\n".join(header + notes)
        evidence = {
            "emails": emails,
            "phones": phones,
            "sites": sites,
            "linkedin": linkedin,
            "blob": dossier,
            "gaps": gaps,
        }
        return emails, dossier[:3200], evidence

    async def audit_contact(self, contact: Contact, *, dry_run: bool = False) -> dict[str, Any]:
        company = contact.company
        company_label = (company.name if company else "") or contact.name or ""
        email = (contact.email or "").strip().lower()
        bounce = dict(self._bounce_by_addr.get(email) or {})
        extra = dict(contact.extra or {})
        if isinstance(extra.get("email_bounce"), dict) and not bounce:
            bounce = dict(extra["email_bounce"])
        mailbox_gone = (bounce.get("classification") or "") == CLASS_MAILBOX_MISSING

        candidates, notes, evidence = await self.gather_candidates(contact)
        verdict = await salvage_saved_contact(
            name=(company.name if company else "") or contact.name or "",
            website=(company.website if company else "") or "",
            email=email,
            city=(company.city if company else "") or "",
            state=(company.state if company else "") or "",
            niche=(company.segment if company else "") or "",
            bounce=bounce,
            candidates=candidates,
            search_notes=notes,
            gaps=list(evidence.get("gaps") or []),
        )

        suggested = normalize_email(str(verdict.get("suggested_email") or ""))
        want_replace = mailbox_gone or _verdict_wants_replace(verdict)
        firm_ok = True
        if company is not None:
            if is_foreign_company(
                name=company.name or "",
                website=company.website or "",
                email=email,
                snippet=str((company.extra or {}).get("snippet") or ""),
            ) or is_junk_lead_name(company.name or ""):
                firm_ok = False
                want_replace = True

        new_email = ""
        if want_replace and firm_ok:
            pool = list(candidates)
            if suggested:
                pool.insert(0, suggested)
            pr = ProviderResult(
                company_name=(company.name if company else "") or contact.name or "",
                website=(company.website if company else "") or "",
                email=email,
                city=(company.city if company else "") or "",
                segment=(company.segment if company else "") or "",
                contact_name=contact.name or "",
            )
            site = (company.website if company else "") or ""
            for cand in pool:
                em = normalize_email(cand)
                if not has_valid_email(em) or em == email:
                    continue
                if em in self._bounce_by_addr:
                    continue
                if not _email_belongs_to_firm(em, site, email):
                    continue
                ok, _why = email_fits_contact(em, pr)
                if ok:
                    new_email = em
                    break
            if not new_email and pool:
                domain = extract_registrable_domain(site)
                if domain and not is_directory_host(domain):
                    guess = pick_best_email(pool, company_domain=domain, require_domain=True)
                    if (
                        guess
                        and guess != email
                        and guess not in self._bounce_by_addr
                        and _email_belongs_to_firm(guess, site, email)
                    ):
                        new_email = guess

        filled: list[str] = []
        applied = False
        contact_id = contact.id
        if not dry_run:
            try:
                filled = await self._fill_gaps(contact, company, verdict, evidence)
                if new_email:
                    applied = await self._swap_email(contact, company, new_email)
                elif want_replace:
                    skip_why = (
                        "empresa_estrangeira"
                        if not firm_ok
                        else "email:invalido"
                    )
                    mark_contact_mail_skip(contact, skip_why)
                    verdict["reason"] = (
                        str(verdict.get("reason") or "")
                        + (
                            " (empresa fora do alvo; mailman não reenvia)"
                            if not firm_ok
                            else " (sem candidato do mesmo domínio; mailman não reenvia)"
                        )
                    )[:160]
                self._mark_reviewed(
                    contact,
                    verdict,
                    bounce=bounce,
                    recovered=bool(new_email and applied),
                    previous=email,
                    new_email=new_email if applied else "",
                    filled=filled,
                )
                await self.session.commit()
            except IntegrityError as exc:
                err = str(getattr(exc, "orig", None) or exc)[:240]
                logger.warning(
                    "auditor_commit_conflict",
                    contact_id=contact_id,
                    error=err,
                )
                await self.session.rollback()
                applied = False
                new_email = ""
                filled = []
                contact = await self.session.get(Contact, contact_id)
                if contact is not None:
                    self._mark_reviewed(
                        contact,
                        verdict,
                        bounce=bounce,
                        recovered=False,
                        previous=email,
                        new_email="",
                        filled=[],
                    )
                    extra = dict(contact.extra or {})
                    blob = dict(extra.get("auditor") or {})
                    blob["commit_error"] = err
                    extra["auditor"] = blob
                    contact.extra = extra
                    self.session.add(contact)
                    await self.session.commit()
                verdict["reason"] = (
                    f"conflito unique (não renomeou): {err}"
                )[:160]
        else:
            filled = await self._fill_gaps(
                contact, company, verdict, evidence, preview=True
            )
        if "clean_name" in filled and (verdict.get("clean_name") or "").strip():
            company_label = str(verdict.get("clean_name") or company_label)

        return {
            "contact_id": contact_id,
            "company": company_label,
            "email": email,
            "new_email": new_email if applied or dry_run else "",
            "bounce": bounce.get("classification") or "",
            "bounce_diag": (bounce.get("diagnostic") or "")[:180],
            "action": "replace" if new_email else "keep",
            "applied": applied,
            "dry_run": dry_run,
            "candidates": candidates[:6],
            "reason": verdict.get("reason"),
            "confidence": verdict.get("confidence"),
            "valid_company": verdict.get("valid_company"),
            "model": verdict.get("model") or get_settings().auditor_model,
            "analysis": (verdict.get("analysis") or "")[:1800],
            "gaps_filled": filled,
            "llm_clean_name": verdict.get("clean_name") or "",
            "llm_phone": verdict.get("phone") or "",
            "llm_city": verdict.get("city") or "",
        }

    async def _fill_gaps(
        self,
        contact: Contact,
        company: Company | None,
        verdict: dict[str, Any],
        evidence: dict[str, Any],
        *,
        preview: bool = False,
    ) -> list[str]:
        """Tapa só coluna vazia, e só com evidência (scrape/busca ou cidade conhecida)."""
        blob = str(evidence.get("blob") or "")
        ev_phones = [p for p in (evidence.get("phones") or []) if p]
        ev_sites = [s for s in (evidence.get("sites") or []) if s]
        ev_li = [u for u in (evidence.get("linkedin") or []) if u]
        filled: list[str] = []

        def take(field: str) -> None:
            if field not in filled:
                filled.append(field)

        phone = ""
        # muitos números no HTML = widget/parceiro; só aceita o que o modelo
        # apontou e que de fato aparece no dossiê
        llm_phone = _normalize_phone(str(verdict.get("phone") or ""))
        if llm_phone and (
            _in_evidence(llm_phone, blob, phone=True)
            or any(_digits(llm_phone) == _digits(p) for p in ev_phones)
        ):
            phone = llm_phone
        elif ev_phones and len(ev_phones) <= 4:
            phone = _normalize_phone(ev_phones[0])
        if phone:
            if company is not None and _blank(company.phone):
                if not preview:
                    company.phone = phone
                take("phone")
            if _blank(contact.phone):
                if not preview:
                    contact.phone = phone
                take("phone")

        if company is not None and _blank(company.website):
            email_hosts = {
                extract_registrable_domain(e.split("@", 1)[-1])
                for e in (evidence.get("emails") or [])
                if "@" in e
            }
            site = _normalize_website(str(verdict.get("website") or ""))
            if site and extract_registrable_domain(site) not in email_hosts:
                if not _in_evidence(extract_registrable_domain(site), blob):
                    site = ""
            if not site:
                for cand in ev_sites:
                    host = extract_registrable_domain(cand)
                    if host and host in email_hosts:
                        site = _normalize_website(cand)
                        if site:
                            break
            if site:
                host = (extract_registrable_domain(site) or "")[:191]
                taken = False
                if host and company.id:
                    taken = await self._company_key_taken(
                        name=company.name or "",
                        city=company.city,
                        website_host=host,
                        exclude_id=company.id,
                    )
                if taken:
                    logger.info(
                        "auditor_skip_website",
                        company_id=company.id,
                        host=host,
                    )
                else:
                    if not preview:
                        company.website = site
                        if host:
                            company.website_host = host
                    take("website")

        if company is not None and _blank(company.city):
            city = str(verdict.get("city") or "").strip()[:128]
            name_blob = f"{company.name or ''} {contact.name or ''}"
            if city and (_in_evidence(city, blob) or _in_evidence(city, name_blob)):
                taken = bool(company.id) and await self._company_key_taken(
                    name=company.name or "",
                    city=city,
                    website_host=company.website_host,
                    exclude_id=company.id,
                )
                if taken:
                    logger.info(
                        "auditor_skip_city",
                        company_id=company.id,
                        city=city,
                    )
                else:
                    if not preview:
                        company.city = city
                    take("city")

        if company is not None and _blank(company.state):
            uf = _normalize_uf(str(verdict.get("state") or ""))
            if not uf and company.city:
                hit = _CITY_INDEX.get(_fold(company.city))
                if hit:
                    uf = hit.state
            if uf:
                if not preview:
                    company.state = uf
                take("state")

        cname = (company.name if company else "") or ""
        if company is not None and _looks_dirty_name(company.name or ""):
            clean = str(verdict.get("clean_name") or "").strip()[:191]
            if not clean:
                host = extract_registrable_domain(
                    (company.website or "") or (ev_sites[0] if ev_sites else "")
                )
                brand = (host.split(".")[0] if host else "").replace("-", " ").strip()
                if brand and len(brand) >= 3 and brand.lower() not in {"www", "mail", "email"}:
                    clean = brand.upper() if len(brand) <= 4 else brand.title()
            if (
                clean
                and len(clean) >= 3
                and not _looks_dirty_name(clean)
                and (
                    _in_evidence(clean, blob)
                    or _fold(clean) in _fold(company.name or "")
                    or _fold(clean).replace(" ", "")
                    in _fold(
                        (company.website or "")
                        + " "
                        + " ".join(ev_sites)
                    )
                )
            ):
                taken = bool(company.id) and await self._company_key_taken(
                    name=clean,
                    city=company.city,
                    website_host=company.website_host,
                    exclude_id=company.id,
                )
                if taken:
                    logger.info(
                        "auditor_skip_rename",
                        company_id=company.id,
                        old=(company.name or "")[:80],
                        new=clean,
                    )
                else:
                    if not preview:
                        company.name = clean
                    take("clean_name")

        if _generic_person(contact.name or "", cname):
            person = str(verdict.get("contact_name") or "").strip()[:255]
            if (
                person
                and len(person.split()) >= 2
                and not _generic_person(person, cname)
                and _in_evidence(person, blob)
            ):
                if not preview:
                    contact.name = person
                take("contact_name")

        if _blank(contact.role):
            role = str(verdict.get("role") or "").strip()[:128]
            if role and 2 <= len(role) <= 80 and _in_evidence(role, blob):
                if not preview:
                    contact.role = role
                take("role")

        if _blank(contact.linkedin):
            li = _normalize_linkedin(str(verdict.get("linkedin") or ""))
            if not li and ev_li:
                li = _normalize_linkedin(ev_li[0])
            if li and (
                "linkedin.com/" in li.lower()
                and (_in_evidence("linkedin.com", blob) or any(li.lower() in u.lower() for u in ev_li))
            ):
                if not preview:
                    contact.linkedin = li
                take("linkedin")

        if not preview:
            self.session.add(contact)
            if company is not None:
                self.session.add(company)
        return filled

    async def _swap_email(
        self, contact: Contact, company: Company | None, new_email: str
    ) -> bool:
        old = (contact.email or "").strip().lower()
        contact.email = new_email
        if company is not None:
            company.email = new_email
        extra = dict(contact.extra or {})
        if extra.get("mailman_skip") in {
            "email_implausivel",
            "bounce",
        } or str(extra.get("mailman_skip") or "").startswith("email:"):
            extra.pop("mailman_skip", None)
            extra.pop("mailman_skip_at", None)
        extra["email_bounce"] = extra.get("email_bounce") or {}
        contact.extra = extra
        self.session.add(contact)
        if company is not None:
            self.session.add(company)

        items = (
            await self.session.execute(
                select(CampaignItem).where(
                    CampaignItem.contact_id == contact.id,
                    CampaignItem.stage.in_(
                        [ItemStageStatus.FAILED.value, ItemStageStatus.DISCARDED.value]
                    ),
                )
            )
        ).scalars().all()
        for item in items:
            item.stage = ItemStageStatus.ENRICHED.value
            item.status = ItemStatus.EMAIL_FOUND.value
            item.error_message = f"auditor recuperou {old} → {new_email}"
        logger.info(
            "auditor_email_replaced",
            contact_id=contact.id,
            old=old,
            new=new_email,
            revived=len(items),
        )
        return True

    def _mark_reviewed(
        self,
        contact: Contact,
        verdict: dict[str, Any],
        *,
        bounce: dict[str, Any],
        recovered: bool,
        previous: str,
        new_email: str = "",
        filled: list[str] | None = None,
    ) -> None:
        extra = dict(contact.extra or {})
        extra["auditor"] = {
            "reviewed_at": _now_iso(),
            "recovered_email": recovered,
            "previous_email": previous,
            "new_email": new_email or None,
            "action": "replace" if recovered else (verdict.get("email_action") or "keep"),
            "reason": verdict.get("reason"),
            "confidence": verdict.get("confidence"),
            "valid_company": verdict.get("valid_company"),
            "bounce": bounce.get("classification") or "",
            "model": verdict.get("model") or get_settings().auditor_model,
            "analysis": (verdict.get("analysis") or "")[:1800],
            "gaps_filled": list(filled or []),
        }
        contact.extra = extra
        self.session.add(contact)
