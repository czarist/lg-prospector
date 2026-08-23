"""Busca grátis: DuckDuckGo (web) + OpenStreetMap/Overpass (local/Maps).

Sem API key. Limites = fair-use dos serviços públicos + rate limit local.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional
from urllib.parse import quote_plus, unquote

import httpx
from bs4 import BeautifulSoup

from app.core.logging import get_logger

logger = get_logger(__name__)

USER_AGENT = "LG-Prospector/0.1 (prospeccao-b2b; +https://local; contact=contato@trentin.software)"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# listagens — não são a empresa
_AGGREGATOR_HOSTS = frozenset(
    {
        "guiatelefone.com",
        "eguias.net",
        "listamais.com.br",
        "telelistas.net",
        "apontador.com.br",
        "guiamais.com.br",
        "econodata.com.br",
        "cnpj.biz",
        "casadosdados.com.br",
        "consultacnpj.com",
        "cnpja.com",
        "yelp.com",
        "tripadvisor.com",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "bing.com",
        "duckduckgo.com",
        "google.com",
        "google.com.br",
        "wikipedia.org",
        "gov.br",
        "inss.gov.br",
        "fazenda.gov.br",
        "receita.fazenda.gov.br",
        "madeiramadeira.com.br",
        "mobly.com.br",
        "casasbahia.com.br",
        "magazineluiza.com.br",
        "americanas.com.br",
        "mercadolivre.com.br",
        "buscaempresa.net",
        "maiscnpj.com.br",
        "significados.com.br",
    }
)
_LOCATION_STOP = frozenset(
    {"brasil", "brazil", "br", "cidade", "estado", "região", "regiao"}
)


def _normalize_result(
    *,
    title: str = "",
    link: str = "",
    snippet: str = "",
    phone: str = "",
    address: str = "",
    website: str = "",
    source: str = "duckduckgo",
) -> dict[str, Any]:
    return {
        "title": title.strip(),
        "name": title.strip(),
        "link": (link or website or "").strip(),
        "website": (website or link or "").strip(),
        "snippet": snippet.strip(),
        "description": snippet.strip(),
        "phoneNumber": phone.strip(),
        "phone": phone.strip(),
        "address": address.strip(),
        "source": source,
    }


# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------

def _host_of(url: str) -> str:
    if not url:
        return ""
    raw = url.strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        from urllib.parse import urlparse

        host = (urlparse(raw).netloc or "").split("@")[-1]
    except Exception:
        return ""
    host = host.split(":")[0].removeprefix("www.")
    return host


def _is_aggregator(url: str, title: str = "") -> bool:
    host = _host_of(url)
    if host in _AGGREGATOR_HOSTS:
        return True
    if any(host.endswith("." + h) for h in _AGGREGATOR_HOSTS):
        return True
    t = (title or "").strip().lower()
    return t.startswith(("10 melhores", "melhores ", "os principais", "top 10"))


def _keep_web(item: dict[str, Any]) -> bool:
    link = item.get("link") or item.get("website") or ""
    title = item.get("title") or item.get("name") or ""
    if not title or not link:
        return False
    if not link.startswith("http"):
        return False
    if _is_aggregator(link, title):
        return False
    low_t = title.lower()
    if "wikipedia" in low_t or "wikipédia" in low_t:
        return False
    from app.providers.geo_email import is_junk_lead_name, keep_brazilian_search_hit

    if is_junk_lead_name(title.split(" - ")[0].split(" | ")[0].strip()):
        return False

    snippet = item.get("snippet") or item.get("description") or ""
    email = (item.get("email") or "").strip()
    return keep_brazilian_search_hit(
        title=title, link=link, snippet=str(snippet), email=email
    )


def _relevance(item: dict[str, Any], query: str) -> int:
    blob = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("snippet") or ""),
            str(item.get("link") or ""),
        ]
    ).lower()
    tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 3]
    if not tokens:
        return 1
    return sum(1 for t in tokens if t in blob)


def _unwrap_bing_link(href: str) -> str:
    """Bing envolve o destino em /ck/a?…&u=a1 + base64."""
    import base64
    from urllib.parse import parse_qs, urlparse, unquote

    if not href:
        return ""
    if "bing.com/ck/" not in href and "bing.com/aclick" not in href:
        return href
    qs = parse_qs(urlparse(href).query)
    raw = (qs.get("u") or [""])[0]
    if not raw:
        return href
    if raw.startswith("a1"):
        raw = raw[2:]
    pad = "=" * ((4 - len(raw) % 4) % 4)
    try:
        dec = base64.urlsafe_b64decode(raw + pad).decode("utf-8", "ignore")
        if dec.startswith("http"):
            return dec
    except Exception:
        pass
    if raw.startswith("http"):
        return unquote(raw)
    return href


def _cite_to_url(cite: str) -> str:
    if not cite:
        return ""
    cite = cite.replace(" › ", "/").replace("»", "/").strip()
    if cite.startswith("http"):
        return cite.split()[0]
    host = cite.split("/")[0].strip().split()[0]
    if "." in host and " " not in host:
        return "https://" + host
    return ""


async def ddg_search(query: str, num: int = 10) -> list[dict[str, Any]]:
    """Busca orgânica. Bing primeiro (rápido); DDG HTML está bloqueado daqui.

    Cascata: 1) Bing HTML  2) lib ddgs (yahoo/bing/startpage)  3) DDG HTML
    """
    from app.core.config import get_settings
    from app.core.limits import search_semaphore, throttle_search

    settings = get_settings()
    max_backends = max(1, min(3, int(settings.search_max_backends)))
    want = max(1, int(num or 10))

    async with search_semaphore():
        await throttle_search()
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _absorb(rows: list[dict[str, Any]]) -> None:
            for item in rows:
                if not _keep_web(item):
                    continue
                key = (item.get("link") or "").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        def _finish() -> list[dict[str, Any]]:
            merged.sort(key=lambda it: _relevance(it, query), reverse=True)
            return merged[:want]

        _absorb(await _bing_html(query, max(want * 2, 12)))
        if len(merged) >= want:
            return _finish()
        if max_backends < 2:
            return _finish()
        await throttle_search()
        _absorb(await _ddg_library(query, max(want * 2, 12)))
        if merged or max_backends < 3:
            return _finish()
        # DDG HTML só se Bing+lib vieram vazios — daqui ele só dá timeout
        await throttle_search()
        _absorb(await _ddg_html(query, want))
        return _finish()


async def _ddg_html(query: str, num: int) -> list[dict[str, Any]]:
    """POST em html.duckduckgo.com/html/ — sem JS. Costuma estar bloqueado."""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"q": query, "b": "", "kl": "br-pt"}
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True, headers=headers) as client:
            resp = await client.post(url, data=data)
            if resp.status_code >= 400:
                logger.warning("ddg_html_status", status=resp.status_code)
                return []
            html = resp.text
    except Exception as exc:
        logger.warning("ddg_html_failed", error=str(exc) or type(exc).__name__)
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    for item in soup.select(".result, .web-result, .results_links"):
        a = item.select_one("a.result__a, a.result-link, a[href]")
        if not a:
            continue
        href = a.get("href") or ""
        title = a.get_text(" ", strip=True)
        # DuckDuckGo usa redirect //duckduckgo.com/l/?uddg=...
        link = _unwrap_ddg_link(href)
        sn_el = item.select_one(".result__snippet, .result-snippet, .snippet")
        snippet = sn_el.get_text(" ", strip=True) if sn_el else ""
        if not title or not link:
            continue
        if "duckduckgo.com" in link and "uddg=" not in href:
            continue
        results.append(
            _normalize_result(title=title, link=link, snippet=snippet, source="duckduckgo")
        )
        if len(results) >= num:
            break

    logger.info("ddg_html_done", query=query, count=len(results))
    return results


def _unwrap_ddg_link(href: str) -> str:
    if not href:
        return ""
    # //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&...
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return unquote(m.group(1))
    if href.startswith("//"):
        href = "https:" + href
    return href


async def _ddg_library(query: str, num: int) -> list[dict[str, Any]]:
    """Fallback: pacote duckduckgo-search / ddgs (sync → thread)."""
    def _run() -> list[dict[str, Any]]:
        try:
            try:
                from ddgs import DDGS  # type: ignore
            except ImportError:
                from duckduckgo_search import DDGS
        except ImportError:
            logger.warning("duckduckgo_search_not_installed")
            return []

        out: list[dict[str, Any]] = []
        try:
            with DDGS() as ddgs:
                # auto varre 15 engines (Google 429, Brave 429, Mojeek 403)
                # e estoura o timeout. yahoo/bing/startpage bastam.
                for item in ddgs.text(
                    query,
                    region="br-pt",
                    max_results=num,
                    backend="yahoo,bing,startpage",
                ):
                    out.append(
                        _normalize_result(
                            title=item.get("title") or "",
                            link=item.get("href") or item.get("link") or "",
                            snippet=item.get("body") or item.get("snippet") or "",
                            source="duckduckgo",
                        )
                    )
        except Exception as exc:
            logger.warning("ddg_library_failed", error=str(exc))
        return out

    results = await asyncio.to_thread(_run)
    logger.info("ddg_library_done", query=query, count=len(results))
    return results


async def _bing_html(query: str, num: int = 10) -> list[dict[str, Any]]:
    """Fallback grátis: scrape da página de resultados do Bing."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count={min(num, 20)}&setlang=pt-BR&cc=BR"
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                logger.warning("bing_html_status", status=resp.status_code)
                return []
            html = resp.text
    except Exception as exc:
        logger.warning("bing_html_failed", error=str(exc))
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        cite_el = li.select_one("cite")
        cite = cite_el.get_text(" ", strip=True) if cite_el else ""
        link = _unwrap_bing_link(href) or _cite_to_url(cite)
        if link.startswith("https://www.bing.com/"):
            link = _cite_to_url(cite) or link
        sn = li.select_one(".b_caption p, p")
        snippet = sn.get_text(" ", strip=True) if sn else ""
        if not title or not link or not link.startswith("http"):
            continue
        results.append(
            _normalize_result(title=title, link=link, snippet=snippet, source="bing")
        )
        if len(results) >= num:
            break
    logger.info("bing_html_done", query=query, count=len(results))
    return results

async def ddg_news(query: str, num: int = 10) -> list[dict[str, Any]]:
    """Notícias via DDG (biblioteca) ou busca com filtro news."""
    def _run() -> list[dict[str, Any]]:
        try:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                from ddgs import DDGS  # type: ignore
        except ImportError:
            return []
        out: list[dict[str, Any]] = []
        try:
            with DDGS() as ddgs:
                for item in ddgs.news(
                    query, region="br-pt", max_results=num, backend="yahoo,bing,startpage"
                ):
                    row = _normalize_result(
                        title=item.get("title") or "",
                        link=item.get("url") or item.get("href") or "",
                        snippet=item.get("body") or item.get("excerpt") or "",
                        source="duckduckgo_news",
                    )
                    publisher = (item.get("source") or "").strip()
                    if publisher:
                        row["publisher"] = publisher
                    out.append(row)
        except Exception as exc:
            logger.warning("ddg_news_failed", error=str(exc))
        return out

    results = await asyncio.to_thread(_run)
    if results:
        return results[:num]
    # fallback: busca web com keyword
    return await ddg_search(f"{query} notícias", num)


# ---------------------------------------------------------------------------
# OpenStreetMap / Nominatim + Overpass
# ---------------------------------------------------------------------------

async def nominatim_geocode(city: str, state: str = "", country: str = "Brasil") -> Optional[dict[str, Any]]:
    """Geocodifica cidade (bbox) via Nominatim — free, com User-Agent obrigatório."""
    from app.domain.cities import is_nationwide

    if not city or is_nationwide(city, state):
        return None
    q = ", ".join(p for p in [city, state, country] if p)
    params = {
        "q": q,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "pt-BR"}
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            resp = await client.get(NOMINATIM_URL, params=params)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            if not data:
                return None
            item = data[0]
            # boundingbox: [south, north, west, east]
            bb = item.get("boundingbox") or []
            if len(bb) == 4:
                south, north, west, east = map(float, bb)
            else:
                lat, lon = float(item["lat"]), float(item["lon"])
                delta = 0.12
                south, north = lat - delta, lat + delta
                west, east = lon - delta, lon + delta
            return {
                "display_name": item.get("display_name"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "south": south,
                "north": north,
                "west": west,
                "east": east,
            }
    except Exception as exc:
        logger.warning("nominatim_failed", city=city, error=str(exc))
        return None


def _osm_keywords(query: str, city: str = "", state: str = "") -> list[str]:
    drop = set(_LOCATION_STOP)
    for part in re.split(r"\W+", f"{city} {state}".lower()):
        if len(part) > 2:
            drop.add(part)
    tokens = [
        t
        for t in re.split(r"\W+", (query or "").lower())
        if len(t) > 2 and t not in drop
    ]
    return tokens[:8]


def _overpass_query_for(query: str, bbox: dict[str, float], limit: int) -> str:
    """POIs na bbox por tag OSM + nome (sem cidade/UF na regex)."""
    s, n, w, e = bbox["south"], bbox["north"], bbox["west"], bbox["east"]
    amenity_tags = _query_to_osm_tags(query)
    parts: list[str] = []
    for key, val in amenity_tags:
        if val == "*":
            parts.append(f'nwr["{key}"]({s},{w},{n},{e});')
        else:
            parts.append(f'nwr["{key}"="{val}"]({s},{w},{n},{e});')
    for token in _osm_keywords(query)[:3]:
        safe = re.sub(r'[\\"\n]', " ", token)[:40].strip()
        if len(safe) >= 4:
            parts.append(f'nwr["name"~"{safe}",i]({s},{w},{n},{e});')
    if not parts:
        parts.append(f'nwr["office"]({s},{w},{n},{e});')
    union = "\n  ".join(parts)
    return f"""
[out:json][timeout:25];
(
  {union}
);
out center {limit};
""".strip()


def _query_to_osm_tags(query: str) -> list[tuple[str, str]]:
    q = query.lower()
    tags: list[tuple[str, str]] = []
    mapping = [
        (["advogad", "advocacia", "jurídic", "juridic", "direito"], ("office", "lawyer")),
        (["contab", "contador"], ("office", "accountant")),
        (["marketing", "publicidade", "agência", "agencia"], ("office", "advertising_agency")),
        (["software", "tecnologia", " informatica", "informática", "empresa de ti"], ("office", "it")),
        (["clínica", "clinica", "médic", "medic", "saúde", "saude"], ("amenity", "clinic")),
        (["restaurante", "bar ", "lanchonete"], ("amenity", "restaurant")),
        (["hotel", "pousada"], ("tourism", "hotel")),
        (["escola", "colégio", "colegio"], ("amenity", "school")),
        (["igreja"], ("amenity", "place_of_worship")),
        (["farmácia", "farmacia"], ("amenity", "pharmacy")),
        (["banco"], ("amenity", "bank")),
        (["jornal", "rádio", "radio", "tv ", "mídia", "midia"], ("office", "newspaper")),
        (["prestador", "consultor"], ("office", "company")),
        (
            ["empresa", "comércio", "comercio", "negócio", "negocio"],
            ("office", "*"),
        ),
        (
            ["empresa", "comércio", "comercio", "loja"],
            ("shop", "supermarket"),
        ),
        (["partido", "político", "politico", "deputado", "senador"], ("office", "political_party")),
    ]
    for keys, tag in mapping:
        if any(k in q for k in keys):
            tags.append(tag)
    if not tags:
        tags.append(("office", "*"))
        tags.append(("shop", "*"))
    # unique preserve order
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:6]


async def overpass_places(
    query: str,
    *,
    city: str = "",
    state: str = "",
    num: int = 20,
) -> list[dict[str, Any]]:
    """POIs locais via Nominatim + Overpass (substitui Google Maps/places)."""
    if not city and not state:
        # sem local: só DDG costuma bastar
        logger.info("overpass_skipped_no_location", query=query)
        return []

    geo = await nominatim_geocode(city or state, state if city else "")
    if not geo:
        logger.warning("overpass_no_geocode", city=city, state=state)
        return []

    # gentileza Nominatim
    await asyncio.sleep(1.0)

    osm_q = " ".join(_osm_keywords(query, city, state)) or query
    ql = _overpass_query_for(osm_q, geo, limit=max(num * 3, 40))
    data = await _overpass_request(ql)
    if not data:
        return []

    elements = data.get("elements") or []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("official_name") or ""
        if not name:
            continue
        website = (
            tags.get("website")
            or tags.get("contact:website")
            or tags.get("url")
            or ""
        )
        phone = tags.get("phone") or tags.get("contact:phone") or ""
        email = tags.get("email") or tags.get("contact:email") or ""
        street = tags.get("addr:street") or ""
        housenumber = tags.get("addr:housenumber") or ""
        city_t = tags.get("addr:city") or city
        state_t = tags.get("addr:state") or state
        address = ", ".join(
            p for p in [f"{street} {housenumber}".strip(), city_t, state_t] if p
        )
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)

        # tag OSM da bbox já localiza; só exige nome bater se a query for bem específica
        keywords = _osm_keywords(query, city, state)
        amenity_tags = _query_to_osm_tags(" ".join(keywords) or query)
        tagged = any(
            (v == "*" and tags.get(k)) or tags.get(k) == v for k, v in amenity_tags
        )
        if not tagged and keywords and not _loose_match(" ".join(keywords), name, tags):
            continue

        item = _normalize_result(
            title=name,
            link=website,
            website=website,
            phone=phone,
            address=address,
            snippet=email or tags.get("description") or address,
            source="openstreetmap",
        )
        if email:
            item["email"] = email
        results.append(item)
        if len(results) >= num:
            break

    logger.info(
        "overpass_done",
        query=query,
        city=city,
        count=len(results),
        area=geo.get("display_name"),
    )
    return results


def _loose_match(query: str, name: str, tags: dict) -> bool:
    q_tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    blob = f"{name} {' '.join(tags.values())}".lower()
    if not q_tokens:
        return True
    hits = sum(1 for t in q_tokens if t in blob)
    return hits >= max(1, len(q_tokens) // 2)


async def _overpass_request(query: str) -> Optional[dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT}
    last_err = None
    for url in OVERPASS_URLS:
        try:
            async with httpx.AsyncClient(timeout=16.0, headers=headers) as client:
                resp = await client.post(url, data={"data": query})
                if resp.status_code >= 400:
                    last_err = f"status={resp.status_code}"
                    continue
                return resp.json()
        except Exception as exc:
            last_err = str(exc)
            continue
    logger.warning("overpass_all_failed", error=last_err)
    return None

# ---------------------------------------------------------------------------
# API unificada free
# ---------------------------------------------------------------------------

async def free_search(
    query: str,
    num: int = 10,
    search_type: str = "search",
    *,
    city: str = "",
    state: str = "",
) -> list[dict[str, Any]]:
    """
    search  → DuckDuckGo
    news    → DuckDuckGo news
    places  → Overpass (se city/state) + DuckDuckGo de reforço
    """
    st = (search_type or "search").lower()
    if st == "news":
        return await ddg_news(query, num)

    if st == "places":
        from app.domain.cities import is_nationwide, search_location

        if is_nationwide(city, state):
            loc = search_location(city, state)
            ddg_q = query if loc.lower() in query.lower() else f"{query} {loc}".strip()
            return await ddg_search(ddg_q, num)
        places = await overpass_places(query, city=city, state=state, num=max(num, 20))
        loc = " ".join(p for p in [city, state, "Brasil"] if p)
        # não duplica cidade se a query do provider já trouxe
        ddg_q = query if loc and loc.lower() in query.lower() else f"{query} {loc}".strip()
        web = await ddg_search(ddg_q, num=num) if len(places) < num else []
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        # OSM com site primeiro — é empresa de verdade, não listagem
        ordered = sorted(
            places,
            key=lambda it: 0 if (it.get("website") or it.get("link") or "").startswith("http") else 1,
        )
        for item in ordered + web:
            key = (item.get("link") or item.get("title") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= num:
                break
        return merged

    # search padrão
    return await ddg_search(query, num)
