"""Provider: prospecção generalista — qualquer empresa BR com presença digital.

Independente dos nichos (advogado, TI, político…). Busca comércio,
serviço, indústria, clínicas, etc. Só entra lead com e-mail válido
e não-público. Origem: prospeccao_generalista.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.domain.entities.provider_result import ProviderResult
from app.domain.interfaces.provider import BaseProvider, SearchContext
from app.providers.base_impl import SearchProviderMixin
from app.providers.email_enrichment import has_valid_email, require_email
from app.providers.geo_email import classify_contact_email, is_plausible_lead, judge_lead
from app.providers.http_tools import build_maps_query, serper_search
from app.providers.opportunity import is_social_url
from app.providers.public_org import is_public_email, is_public_organ

logger = get_logger(__name__)

ORIGIN = "prospeccao_generalista"

_SECTOR_QUERIES = (
    "empresa comércio site contato",
    "prestador de serviço empresa site",
    "clínica consultório site",
    "imobiliária site contato",
    "restaurante site institucional",
    "oficina mecânica site",
    "loja site contato",
    "distribuidora atacado site",
    "indústria empresa site",
    "hotel pousada site",
    "escritório empresa site contato",
    "academia estética salão site",
    "farmácia drogaria site contato",
    "construtora engenharia site",
    "transportadora logística site",
    "pet shop veterinária site",
    "padaria supermercado site",
    "autopeças pneus site",
    "gráfica comunicação visual site",
    "contabilidade escritório site",
    "óptica joalheria site",
    "escola curso profissionalizante site",
)


class GeneralistaProvider(SearchProviderMixin, BaseProvider):
    code = "generalista"
    name = "Prospecção generalista"
    niche = "generalista"
    template_file = "email-prospeccao-generalista.html"
    strategies = ["google_maps", "google_search", "presenca_digital"]
    segment = "generalista"
    source_label = ORIGIN

    def _queries(self, ctx: SearchContext) -> list[str]:
        from app.domain.cities import search_location

        city = (ctx.city or "").strip()
        state = (ctx.state or "").strip()
        loc = search_location(city, state)
        q = (ctx.query or "").strip()
        out: list[str] = []
        if q:
            out.append(f"{q} {loc}".strip())
        seed = sum(ord(c) for c in (city or state or "br"))
        n = len(_SECTOR_QUERIES)
        round_idx = int((ctx.extra or {}).get("discover_round") or 0)
        # cada visita / retry começa noutro setor — não recicla o mesmo SERP
        start = (seed + round_idx * 5) % n
        take = min(n, 8)
        picked = [_SECTOR_QUERIES[(start + i) % n] for i in range(take)]
        for p in picked:
            out.append(f"{p} {loc}".strip())
        return [x for x in out if len(x) > 8]

    def _normalize_hit(self, item: dict, ctx: SearchContext) -> ProviderResult | None:
        title = self._clean_title(item.get("title") or item.get("name") or "")
        link = item.get("website") or item.get("link") or item.get("url") or ""
        snippet = item.get("snippet") or item.get("description") or ""
        if not title:
            return None
        extra = {"snippet": snippet, "origin": ORIGIN, "raw": item}
        website = link
        if link and is_social_url(link):
            extra["social"] = link
            website = ""
        pr = ProviderResult(
            company_name=title,
            website=website,
            phone=item.get("phoneNumber") or item.get("phone") or "",
            email=(item.get("email") or "").strip(),
            city=ctx.city or self._parse_location(item.get("address") or "")[0],
            state=ctx.state or self._parse_location(item.get("address") or "")[1],
            segment=self.segment,
            source=ORIGIN,
            extra=extra,
        )
        if not is_plausible_lead(
            name=pr.company_name,
            website=pr.website,
            email=pr.email,
            snippet=snippet,
            segment=self.segment,
        ):
            return None
        if is_public_organ(
            name=pr.company_name,
            website=pr.website,
            email=pr.email,
            snippet=snippet,
            segment=self.segment,
            allow_gov_br=True,
        ):
            return None
        return pr

    async def search_companies(self, ctx: SearchContext) -> list[ProviderResult]:
        from app.providers.domain_email import extract_registrable_domain

        pool: list[ProviderResult] = []
        seen: set[str] = set()
        target = max(ctx.max_results * 3, 12)
        extra_ctx = ctx.extra or {}
        exclude_hosts = {
            str(h).strip().lower()
            for h in (extra_ctx.get("exclude_hosts") or [])
            if h
        }
        exclude_names = {
            str(n).strip().lower()
            for n in (extra_ctx.get("exclude_names") or [])
            if n
        }
        exclude_emails = {
            str(e).strip().lower()
            for e in (extra_ctx.get("exclude_emails") or [])
            if e
        }

        def _is_known(pr: ProviderResult) -> bool:
            host = extract_registrable_domain(pr.website or "")
            if host and host.lower() in exclude_hosts:
                return True
            name = (pr.company_name or "").strip().lower()
            if name and name in exclude_names:
                return True
            em = (pr.email or "").strip().lower()
            if em and em in exclude_emails:
                return True
            return False

        def _add(pr: ProviderResult | None) -> None:
            if not pr or _is_known(pr):
                return
            if not pr.is_valid_company():
                return
            key = pr.normalize_key()
            if key in seen:
                return
            seen.add(key)
            pool.append(pr)

        maps_q = build_maps_query(
            ctx.query or "empresa comércio serviços", ctx.city, ctx.state
        )
        places = await serper_search(
            maps_q,
            num=max(target // 2, 10),
            search_type="places",
            city=ctx.city,
            state=ctx.state,
        )
        for item in places:
            _add(self._normalize_hit(item, ctx))
            if len(pool) >= target:
                break

        for q in self._queries(ctx):
            if len(pool) >= target:
                break
            more = await self._search_organic(
                q,
                max(8, ctx.max_results),
                city=ctx.city,
                state=ctx.state,
                ctx=ctx,
            )
            for r in more:
                r.segment = self.segment
                r.source = ORIGIN
                extra = dict(r.extra or {})
                extra["origin"] = ORIGIN
                if r.website and is_social_url(r.website):
                    extra["social"] = r.website
                    r.website = ""
                r.extra = extra
                _add(r)

        # resolve e-mail agora: generalista só cadastra com contato
        results: list[ProviderResult] = []
        seen_email: set[str] = set()
        max_resolve = min(len(pool), max(ctx.max_results * 3, 10))
        for pr in pool[:max_resolve]:
            if len(results) >= ctx.max_results:
                break
            kept = await require_email(
                pr, deep=True, require_domain=False, allow_free_mail=True
            )
            if not kept or not has_valid_email(kept.email):
                continue
            if is_public_email(kept.email, allow_gov_br=True):
                continue
            ok, reason = classify_contact_email(
                kept.email,
                name=kept.company_name,
                city=kept.city,
                website=kept.website,
                segment=self.segment,
            )
            if not ok:
                logger.info(
                    "generalista_email_rejeitado",
                    email=kept.email,
                    reason=reason,
                    company=kept.company_name,
                )
                continue
            lead_ok, lead_why = judge_lead(
                name=kept.company_name,
                email=kept.email,
                website=kept.website,
                city=kept.city,
                segment=self.segment,
                snippet=str((kept.extra or {}).get("snippet") or ""),
            )
            if not lead_ok:
                logger.info(
                    "generalista_lead_rejeitado",
                    company=kept.company_name,
                    reasons=lead_why,
                )
                continue
            em = kept.email.lower()
            if em in seen_email or em in exclude_emails:
                continue
            seen_email.add(em)
            extra = dict(kept.extra or {})
            extra["origin"] = ORIGIN
            kept.extra = extra
            kept.source = ORIGIN
            kept.segment = self.segment
            results.append(kept)

        logger.info(
            "generalista_search_done",
            city=ctx.city,
            state=ctx.state,
            pool=len(pool),
            with_email=len(results),
        )
        return results[: ctx.max_results]
