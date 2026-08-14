"""Provider: Políticos / Partidos — campanha e partido (nunca órgão público).

Fontes:
  1. TSE / DivulgaCandContas — candidatos municipais (prefeito/vereador)
  2. Busca web — diretórios partidários e campanhas

Regra de ouro: **só entra lead com e-mail válido e não-público**.
O TSE não publica e-mail na listagem; para cada candidato buscamos contato
na web e descartamos quem não tiver.

Template: email-prospeccao-politicos.html
"""

from __future__ import annotations

import asyncio
import re

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.email_enrichment import has_valid_email, normalize_email
from app.providers.geo_email import (
    classify_contact_email,
    is_plausible_br_website,
    serp_result_relevant_to_person,
)
from app.providers.http_tools import serper_search
from app.providers.public_org import (
    PARTIDOS_BR,
    is_politico_target,
    is_public_email,
    negative_search_tokens_public,
)
from app.providers.scraper import extract_emails
from app.providers.tse_candidatos import TseCandidate, fetch_candidates_for_city

logger = get_logger(__name__)

_EMAIL_IN_TEXT = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)


class PoliticosProvider(SearchProviderMixin, BaseProvider):
    code = "politicos"
    name = "Políticos e Partidos"
    niche = "politico"
    template_file = "email-prospeccao-politicos.html"
    strategies = [
        "tse_divulgacand",
        "diretorios_partidarios",
        "campanhas_eleitorais",
        "sites_partidos",
        "comites_locais",
    ]
    segment = "politico"
    source_label = "partidos_campanha"

    def _location(self, ctx: SearchContext) -> str:
        return " ".join(x for x in ((ctx.city or "").strip(), (ctx.state or "").strip()) if x)

    def _neg(self) -> str:
        return (
            f"{negative_search_tokens_public()} "
            f'-"assembleia legislativa" -"câmara municipal" -"camara municipal" '
            f'-prefeitura -"gabinete do deputado" -"portal da câmara" '
            f'-senado -"câmara dos deputados"'
        )

    def _build_queries(self, ctx: SearchContext) -> list[str]:
        q = (ctx.query or "").strip()
        loc = self._location(ctx)
        city = (ctx.city or "").strip()
        state = (ctx.state or "").strip()
        neg = self._neg()

        queries: list[str] = []
        if q:
            queries.append(f"{q} {loc} partido diretório contato email {neg}".strip())
            queries.append(f"{q} {loc} campanha site contato email {neg}".strip())
        if city:
            queries.append(f"diretório municipal partido {city} {state} contato email {neg}".strip())
            queries.append(f"comissão provisória partido {city} contato email {neg}".strip())
            queries.append(f"candidato prefeito {city} campanha email contato {neg}".strip())
        if state:
            queries.append(f"diretório estadual partido {state} contato email {neg}".strip())

        if city or state:
            seed = sum(ord(c) for c in (city or state))
            n = len(PARTIDOS_BR)
            start = seed % max(1, n)
            picked = [PARTIDOS_BR[(start + i * 3) % n] for i in range(min(3, n))]
            for p in picked:
                sigla = p["sigla"]
                if city:
                    queries.append(
                        f'diretório {sigla} {city} (contato OR email OR "comissão provisória") {neg}'
                    )
                else:
                    queries.append(f"diretório estadual {sigla} {state} contato email {neg}")

        queries.append(f"equipe de campanha {loc} email contato {neg}".strip())

        seen: set[str] = set()
        out: list[str] = []
        for item in queries:
            key = " ".join(item.lower().split())
            if key in seen or len(key) < 12:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _pick_private_email(
        self,
        candidates: list[str],
        *,
        name: str = "",
        city: str = "",
        party: str = "",
        website: str = "",
    ) -> str:
        for raw in candidates:
            em = normalize_email(raw)
            if not has_valid_email(em) or is_public_email(em):
                continue
            ok, reason = classify_contact_email(
                em,
                name=name,
                city=city,
                party=party,
                website=website,
                segment=self.segment,
            )
            if not ok:
                logger.info(
                    "politico_email_rejected",
                    email=em,
                    name=name or None,
                    reason=reason,
                )
                continue
            return em
        return ""

    def _emails_from_organic(
        self,
        organic: list[dict],
        *,
        name: str = "",
        city: str = "",
        state: str = "",
        party: str = "",
        require_person: bool = False,
    ) -> tuple[str, str]:
        """Retorna (email, website_hint) a partir de resultados SERP.

        require_person=True (TSE): só lê snippet de resultado que fala
        da pessoa + cidade/partido/campanha. Evita AltaVista/laramie,
        ana.co.jp, github.com/rchiodo etc.
        """
        emails: list[str] = []
        website = ""
        for item in organic:
            title = item.get("title") or ""
            snippet = f"{title} {item.get('snippet') or ''} {item.get('description') or ''}"
            link = item.get("link") or item.get("website") or ""
            if require_person and name:
                if not serp_result_relevant_to_person(
                    title,
                    snippet,
                    link,
                    name=name,
                    city=city,
                    state=state,
                    party=party,
                ):
                    continue
            emails.extend(extract_emails(snippet))
            emails.extend(_EMAIL_IN_TEXT.findall(snippet))
            if not website and link and is_plausible_br_website(link):
                website = link
        email = self._pick_private_email(
            emails, name=name, city=city, party=party, website=website
        )
        return email, website

    async def _resolve_email_for_candidate(self, cand: TseCandidate) -> tuple[str, str]:
        """Busca e-mail (e site) na web para um candidato TSE. Vazio se não achar."""
        name = cand.nome_urna or cand.nome_completo
        party = cand.partido_sigla
        city = cand.city
        state = cand.state
        neg = self._neg()
        queries = [
            f'"{name}" {city} {state} (email OR contato OR "e-mail") campanha {neg}',
            f'"{name}" {party} {city} campanha contato email {neg}',
            f'candidato {cand.cargo} "{name}" {city} email {neg}',
        ]
        person = " ".join(x for x in (name, cand.nome_completo) if x)
        for q in queries:
            organic = await serper_search(q, num=8, city=city, state=state)
            email, website = self._emails_from_organic(
                organic,
                name=person,
                city=city,
                state=state,
                party=party,
                require_person=True,
            )
            if email:
                logger.info(
                    "tse_candidate_email_found",
                    name=name,
                    party=party,
                    email=email,
                    city=city,
                )
                return email, website
            await asyncio.sleep(0.3)
        logger.info("tse_candidate_no_email", name=name, party=party, city=city)
        return "", ""

    async def _from_tse(self, ctx: SearchContext) -> list[ProviderResult]:
        if not ctx.city or not ctx.state:
            return []
        # pede mais candidatos do que o alvo: a maioria não terá e-mail público
        pool = max(ctx.max_results * 4, 20)
        try:
            cands = await fetch_candidates_for_city(
                ctx.city, ctx.state, max_results=pool
            )
        except Exception as exc:
            logger.warning("tse_fetch_error", error=str(exc), city=ctx.city)
            return []

        # prioriza prefeito, depois vereador
        cands.sort(key=lambda c: (0 if c.cargo_codigo == 11 else 1, c.nome_urna))

        results: list[ProviderResult] = []
        # limita buscas de e-mail (custo SERP)
        max_lookups = min(len(cands), max(ctx.max_results * 3, 12))
        for cand in cands[:max_lookups]:
            if len(results) >= ctx.max_results:
                break
            email, website = await self._resolve_email_for_candidate(cand)
            if not email:
                continue  # sem e-mail → não entra
            pr = ProviderResult(
                company_name=cand.label[:191],
                contact_name=(cand.nome_completo or cand.nome_urna)[:255],
                email=email,
                website=website or "",
                city=cand.city or ctx.city,
                state=cand.state or ctx.state,
                segment=self.segment,
                source="tse_divulgacand",
                extra={
                    "tse_id": cand.id,
                    "tse_cargo": cand.cargo,
                    "tse_partido": cand.partido_sigla,
                    "tse_numero": cand.numero,
                    "tse_situacao": cand.situacao,
                    "snippet": f"Candidato TSE {cand.cargo} {cand.partido_sigla} {cand.city}",
                    **(cand.extra or {}),
                },
            )
            ok_email, reason = classify_contact_email(
                pr.email,
                name=pr.contact_name or pr.company_name,
                city=pr.city,
                party=cand.partido_sigla,
                website=pr.website,
                segment=self.segment,
            )
            if not ok_email:
                logger.info(
                    "tse_candidate_email_implausible",
                    name=pr.contact_name,
                    email=pr.email,
                    reason=reason,
                )
                continue
            if pr.is_valid_company() and has_valid_email(pr.email) and not is_public_email(pr.email):
                results.append(pr)
        return results

    async def _from_web(self, ctx: SearchContext, already: int) -> list[ProviderResult]:
        """Diretórios/campanhas via SERP — só mantém se o snippet já trouxer e-mail
        ou se o site parecer partido/campanha (enrich aprofunda e-mail depois).

        Para cumprir "garantir e-mail", aqui também exigimos e-mail no snippet
        quando possível; se só houver site de partido, deixa o enrich resolver
        (require_email). Na prática buscamos queries com 'email'.
        """
        need = max(0, ctx.max_results - already)
        if need <= 0:
            return []

        results: list[ProviderResult] = []
        seen: set[str] = set()
        for query in self._build_queries(ctx):
            if len(results) >= need:
                break
            organic = await serper_search(
                query, num=min(10, need + 4), city=ctx.city, state=ctx.state
            )
            for item in organic:
                title = self._clean_title(item.get("title") or "")
                link = item.get("link") or item.get("website") or ""
                snippet = item.get("snippet") or item.get("description") or ""
                if not title:
                    continue
                if not is_politico_target(name=title, website=link, snippet=snippet):
                    continue
                email = self._pick_private_email(
                    extract_emails(f"{title} {snippet}") + _EMAIL_IN_TEXT.findall(snippet),
                    name=title,
                    city=ctx.city or "",
                    website=link,
                )
                # sem e-mail no SERP: ainda aceita se for claramente partido/campanha
                # com site próprio — enrich vai tentar (e descarta se falhar)
                if not email and not link:
                    continue
                key = f"{title.lower()}|{link.lower()}|{email}"
                if key in seen:
                    continue
                seen.add(key)

                qlow = query.lower()
                if "diretório" in qlow or "partido" in qlow:
                    source = "diretorios_partidarios"
                elif "campanha" in qlow or "candidato" in qlow:
                    source = "campanhas_eleitorais"
                else:
                    source = self.source_label

                pr = ProviderResult(
                    company_name=title,
                    contact_name=title,
                    email=email,
                    website=link,
                    city=ctx.city,
                    state=ctx.state,
                    segment=self.segment,
                    source=source,
                    extra={"snippet": snippet, "query": query[:120]},
                )
                # se já tem e-mail → ok; se não, só com website e válido
                if email:
                    if not has_valid_email(email) or is_public_email(email):
                        continue
                elif not pr.is_valid_company():
                    continue
                else:
                    # sem e-mail: só passa se is_politico_target ok (já checado)
                    pass
                if not pr.is_valid_company() and not email:
                    continue
                # forçar validação político
                if not is_politico_target(
                    name=pr.company_name, website=pr.website, email=pr.email, snippet=snippet
                ):
                    continue
                results.append(pr)
                if len(results) >= need:
                    break
        return results

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        seen: set[str] = set()

        # 1) TSE — só com e-mail resolvido
        tse = await self._from_tse(ctx)
        for pr in tse:
            key = pr.normalize_key() + "|" + (pr.email or "")
            if key in seen:
                continue
            seen.add(key)
            results.append(pr)

        # 2) Web (partidos/campanhas) — completa a cota
        if len(results) < ctx.max_results:
            web = await self._from_web(ctx, already=len(results))
            for pr in web:
                key = pr.normalize_key() + "|" + (pr.email or "")
                if key in seen:
                    continue
                # web sem e-mail: ok se site; enrich exige e-mail depois
                if pr.email and is_public_email(pr.email):
                    continue
                seen.add(key)
                results.append(pr)
                if len(results) >= ctx.max_results:
                    break

        logger.info(
            "politicos_search_done",
            city=ctx.city,
            state=ctx.state,
            total=len(results),
            with_email=sum(1 for r in results if r.email),
            from_tse=sum(1 for r in results if r.source == "tse_divulgacand"),
        )
        return results[: ctx.max_results]
