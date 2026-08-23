"""Provider: Advogados — escritórios privados com e-mail garantido.

Fontes públicas / semi-públicas (sem scrape em massa da OAB):
  - Google Maps / Places ou Overpass/OSM (free)
  - Busca orgânica: sociedade de advogados, .adv.br, diretórios locais
  - Negativos: -gov -leg -defensoria -MP -TJ -OAB institucional

Regra de ouro (igual políticos/TSE): **só entra lead com e-mail válido
e não-público**. Sem e-mail → não vai pro enrich.

Não faz scraping em massa da OAB (ToS). Jusbrasil/Escavador = lixo
em is_valid_company (perfil ≠ site do escritório).
"""

from __future__ import annotations

import asyncio
import re

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.email_enrichment import (
    has_valid_email,
    normalize_email,
    require_email,
)
from app.providers.geo_email import classify_contact_email
from app.providers.http_tools import build_maps_query, serper_search
from app.providers.public_org import is_public_email, negative_search_tokens_public
from app.providers.scraper import extract_emails

logger = get_logger(__name__)

_EMAIL_IN_TEXT = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)


class AdvogadosProvider(SearchProviderMixin, BaseProvider):
    code = "advogados"
    name = "Advogados"
    niche = "advogado"
    template_file = "email-prospeccao-advogados.html"
    strategies = [
        "google_maps",
        "osm_overpass",
        "google_search",
        "diretorios_locais",
        "adv_br",
        "email_resolve",
    ]
    segment = "advogado"
    source_label = "google"

    def _neg(self) -> str:
        return (
            f"{negative_search_tokens_public()} "
            f'-defensoria -"ministério público" -promotoria '
            f'-"ordem dos advogados" -"oab seccional" -"conselho federal" '
            f'-"tribunal de justiça" -fórum -forum -jusbrasil -escavador'
        )

    def _organic_queries(self, q: str, ctx: SearchContext) -> list[str]:
        from app.domain.cities import is_nationwide, search_location

        city = (ctx.city or "").strip()
        state = (ctx.state or "").strip()
        loc = search_location(city, state)
        municipal = bool(city) and not is_nationwide(city, state)
        base = (q or "escritório de advocacia").strip()
        neg = self._neg()
        variants = [
            f"{base} {loc} escritório (contato OR email) site {neg}".strip(),
            f"sociedade de advogados {loc} (email OR contato) {neg}".strip(),
            f"advogados associados {loc} site contato {neg}".strip(),
            f"escritório advocacia empresarial {loc} email {neg}".strip(),
            # TLD típico de escritório BR
            f'site:.adv.br "{city}" advocacia contato {neg}'.strip() if municipal else f"site:.adv.br advocacia {loc} {neg}".strip(),
            f'"{city}" "sociedade de advogados" (email OR "@") {neg}'.strip() if municipal else "",
            f"advogado trabalhista {loc} escritório email site {neg}".strip(),
            f"advogado cível {loc} escritório contato {neg}".strip(),
            f"advocacia {loc} \"@\" contato -gov {neg}".strip(),
        ]
        return [v for v in variants if v and len(v) > 8]

    def _pick_private_email(
        self,
        candidates: list[str],
        *,
        name: str = "",
        city: str = "",
        website: str = "",
    ) -> str:
        for raw in candidates:
            em = normalize_email(raw)
            if not has_valid_email(em) or is_public_email(em):
                continue
            ok, _reason = classify_contact_email(
                em,
                name=name,
                city=city,
                website=website,
                segment=self.segment,
            )
            if ok:
                return em
        return ""

    def _emails_from_blob(self, *parts: str) -> list[str]:
        blob = " ".join(p for p in parts if p)
        found = extract_emails(blob) + _EMAIL_IN_TEXT.findall(blob)
        return found

    async def _collect_candidates(self, ctx: SearchContext) -> list[ProviderResult]:
        """Maps + orgânico — pool maior; e-mail ainda não exigido aqui."""
        q = ctx.query or "escritório de advocacia"
        pool: list[ProviderResult] = []
        seen: set[str] = set()
        target = max(ctx.max_results * 4, 16)

        def _add(pr: ProviderResult) -> None:
            pr.segment = self.segment
            if not pr.is_valid_company() or self._is_known_lead(pr, ctx):
                return
            key = pr.normalize_key()
            if key in seen:
                return
            seen.add(key)
            # se snippet já trouxe e-mail, aproveita
            if not pr.email:
                extra = pr.extra or {}
                snip = str(extra.get("snippet") or "")
                em = self._pick_private_email(
                    self._emails_from_blob(pr.company_name, snip, pr.email),
                    name=pr.company_name,
                    city=pr.city,
                    website=pr.website,
                )
                if em:
                    pr.email = em
            pool.append(pr)

        # 1) Maps / Places (ou Overpass no backend free)
        maps_q = build_maps_query(f"{q} advogado escritório", ctx.city, ctx.state)
        places = await serper_search(
            maps_q,
            num=max(target // 2, 12),
            search_type="places",
            city=ctx.city,
            state=ctx.state,
        )
        for item in places:
            title = item.get("title") or item.get("name") or ""
            snip = item.get("snippet") or item.get("description") or ""
            _add(
                ProviderResult(
                    company_name=self._clean_title(title),
                    website=item.get("website") or item.get("link") or "",
                    phone=item.get("phoneNumber") or item.get("phone") or "",
                    email=self._pick_private_email(
                        self._emails_from_blob(title, snip, item.get("email") or ""),
                        name=title,
                        city=ctx.city or "",
                        website=item.get("website") or item.get("link") or "",
                    ),
                    city=ctx.city or self._parse_location(item.get("address") or "")[0],
                    state=ctx.state or self._parse_location(item.get("address") or "")[1],
                    segment=self.segment,
                    source=item.get("source") or "google_maps",
                    extra={
                        "address": item.get("address"),
                        "snippet": snip,
                        "raw": item,
                    },
                )
            )
            if len(pool) >= target:
                return pool[:target]

        # 2) Orgânico multi-query
        for search_q in self._organic_queries(q, ctx):
            if len(pool) >= target:
                break
            need = target - len(pool) + 4
            more = await self._search_organic(
                search_q, need, city=ctx.city, state=ctx.state, ctx=ctx
            )
            for r in more:
                r.segment = self.segment
                r.source = r.source or "google_search"
                snip = str((r.extra or {}).get("snippet") or "")
                if not r.email:
                    r.email = self._pick_private_email(
                        self._emails_from_blob(r.company_name, snip, r.website),
                        name=r.company_name,
                        city=r.city or ctx.city or "",
                        website=r.website,
                    )
                _add(r)

        return pool[:target]

    async def _resolve_email(self, pr: ProviderResult) -> ProviderResult | None:
        """Garante e-mail: scrape multi-pass no site e/ou SERP. None se falhar."""
        if has_valid_email(pr.email) and not is_public_email(pr.email):
            pr.email = normalize_email(pr.email)
            ok, _reason = classify_contact_email(
                pr.email,
                name=pr.company_name,
                city=pr.city or "",
                website=pr.website or "",
                segment=self.segment,
            )
            if ok:
                return pr
            pr.email = ""

        # 1) site do escritório → multi-pass (domínio preferido; free-mail só se sem domínio)
        if pr.website:
            kept = await require_email(
                pr,
                deep=True,
                require_domain=False,  # aceita contato@ se achar no site
                allow_free_mail=False,  # preferir e-mail do site; free só no passo 2
            )
            if kept and has_valid_email(kept.email) and not is_public_email(kept.email):
                return kept

        # 2) SERP focado em e-mail do nome + cidade
        name = pr.company_name
        city = pr.city or ""
        state = pr.state or ""
        neg = self._neg()
        queries = [
            f'"{name}" {city} (email OR contato OR "e-mail" OR @) advocacia {neg}',
            f'"{name}" {city} {state} escritório contato email {neg}',
        ]
        if pr.website:
            from app.providers.domain_email import extract_registrable_domain

            dom = extract_registrable_domain(pr.website)
            if dom:
                queries.insert(0, f'"{name}" ("@{dom}" OR contact OR email) {neg}')

        for q in queries:
            organic = await serper_search(q, num=8, city=city, state=state)
            emails: list[str] = []
            for item in organic:
                blob = f"{item.get('title') or ''} {item.get('snippet') or ''} {item.get('link') or ''}"
                emails.extend(self._emails_from_blob(blob))
                # se achar site melhor
                link = item.get("link") or ""
                if link and not pr.website:
                    low = link.lower()
                    if not any(
                        x in low
                        for x in (
                            "facebook.com",
                            "instagram.com",
                            "jusbrasil",
                            "escavador",
                            "linkedin.com",
                            "wikipedia",
                        )
                    ):
                        pr.website = link
            em = self._pick_private_email(
                emails,
                name=name,
                city=city,
                website=pr.website or "",
            )
            if em:
                pr.email = em
                logger.info(
                    "advogado_email_found_serp",
                    company=name,
                    email=em,
                    city=city,
                )
                return pr
            await asyncio.sleep(0.25)

        # 3) última tentativa: free-mail liberado se scrape do site achar gmail
        if pr.website:
            kept = await require_email(
                pr, deep=True, require_domain=False, allow_free_mail=True
            )
            if kept and has_valid_email(kept.email) and not is_public_email(kept.email):
                return kept

        logger.info("advogado_no_email", company=name, website=pr.website or None, city=city)
        return None

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        pool = await self._collect_candidates(ctx)
        # quem já tem e-mail primeiro (barato)
        pool.sort(
            key=lambda p: (
                0 if has_valid_email(p.email) and not is_public_email(p.email or "") else 1,
                0 if p.website else 1,
            )
        )

        results: list[ProviderResult] = []
        seen_email: set[str] = set()
        # limita resoluções caras
        max_resolve = min(len(pool), max(ctx.max_results * 3, 12))

        for pr in pool[:max_resolve]:
            if len(results) >= ctx.max_results:
                break
            resolved = await self._resolve_email(pr)
            if not resolved:
                continue
            em = normalize_email(resolved.email)
            if not has_valid_email(em) or is_public_email(em):
                continue
            _, _, exclude_emails = self._known_sets(ctx)
            if em in seen_email or em in exclude_emails:
                continue
            seen_email.add(em)
            resolved.email = em
            resolved.segment = self.segment
            if not resolved.is_valid_company():
                continue
            results.append(resolved)

        logger.info(
            "advogados_search_done",
            city=ctx.city,
            state=ctx.state,
            pool=len(pool),
            with_email=len(results),
        )
        return results[: ctx.max_results]
