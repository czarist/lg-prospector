"""Validação geográfica / de pertinência de e-mail e site.

O prospector é BR. A checagem DNS (MX existe) NÃO basta: um e-mail
americano ou japonês com MX válido passava e era associado a candidato
TSE só porque o nome colidia no SERP (ex.: ALTAVISTA → laramie1.org,
ALAN MONTORO → alanchikinchow.com).

Regras:
  - ccTLD estrangeiro / .edu/.gov/.mil sem .br → rejeita
  - redes sociais, dicionários de nome, diretórios → rejeita
  - nicho pessoa (político/advogado): o local ou o domínio tem que
    bater com o nome (ou ser *.br com caixa genérica contato@)
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

from app.providers.domain_email import (
    FREE_MAIL,
    extract_registrable_domain,
    is_free_mail,
    matches_company_domain,
)

# ccTLDs / sufixos que nunca são lead BR
_FOREIGN_SUFFIXES: tuple[str, ...] = (
    ".edu",
    ".gov",
    ".mil",
    ".int",
    ".gl",
    ".jp",
    ".co.jp",
    ".ne.jp",
    ".or.jp",
    ".uy",
    ".uk",
    ".co.uk",
    ".us",
    ".au",
    ".ca",
    ".de",
    ".fr",
    ".it",
    ".es",
    ".ru",
    ".cn",
    ".in",
    ".mx",
    ".ar",
    ".cl",
    ".pe",
    ".bo",
    ".ec",
    ".ve",
    ".py",
    ".nz",
    ".ie",
    ".nl",
    ".be",
    ".ch",
    ".se",
    ".no",
    ".dk",
    ".fi",
    ".pl",
    ".kr",
    ".tw",
    ".hk",
    ".sg",
    ".th",
    ".ph",
    ".id",
    ".za",
    ".ao",
    ".mz",
    ".pt",  # Portugal — não é o nicho
    ".co.jp",
)

# hosts de e-mail que nunca são contato comercial/campanha
JUNK_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "facebook.com",
        "messenger.com",
        "instagram.com",
        "mail.instagram.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "youtube.com",
        "dailymotion.com",
        "github.com",
        "gitlab.com",
        "nameberry.com",
        "behindthename.com",
        "wikipedia.org",
        "wikimedia.org",
        "google.com",
        "googlemail.com",  # ok as free-mail via FREE_MAIL? googlemail is in FREE_MAIL
        "apple.com",
        "microsoft.com",
        "amazon.com",
        "linkedin.com",
        "live.com",  # outlook.live.com paths; live.com itself is FREE_MAIL
        "heraldbulletin.com",
        "fordmodels.com",
        "ana.org",
        "ana.co.jp",
        "econodata.com.br",
        "serasaexperian.com.br",
        "cnpj.biz",
        "cnpja.com",
        # diretórios / classificados / vagas — nunca são o contato da empresa
        "juridicocerto.com",
        "previdenciarista.com",
        "lawzana.com",
        "guiafacil.com",
        "guiatelefone.com",
        "guiamania.com.br",
        "ohub.com.br",
        "econodata.com.br",
        "canaldoanuncio.com",
        "contaazul.com",
        "nibo.com.br",
        "contabilidade.com",
        "infojobs.com.br",
        "catho.com.br",
        "indeed.com",
        "jobted.com",
        "glassdoor.com",
        "glassdoor.com.br",
        "bebee.com",
        "trabalhabrasil.com.br",
        "vagas.com.br",
        "solutudo.com.br",
        "telelistas.net",
        "encontrabrasil.com.br",
        "eguias.net",
        "applocal.com.br",
        "viatapida.com",
        "comerciosaopaulo.com.br",
        "mestres.app",
        "mestresdosite.com.br",
        "withnocode.io",
        "babylovegrowth.ai",
        "juriscorrespondente.com.br",
        "webdesignbrasil.org",
        "msn.com",
        "sapo.pt",
        "radiosaovivo.net",
        "tudoradio.com",
    }
    - {"googlemail.com", "live.com"}  # free-mail legítimo
)

# hosts que são diretório/classificado (empresa ≠ o domínio)
DIRECTORY_HOSTS: frozenset[str] = frozenset(
    {
        "juridicocerto.com",
        "previdenciarista.com",
        "lawzana.com",
        "guiafacil.com",
        "guiatelefone.com",
        "guiamania.com.br",
        "ohub.com.br",
        "econodata.com.br",
        "canaldoanuncio.com",
        "contaazul.com",
        "nibo.com.br",
        "contabilidade.com",
        "infojobs.com.br",
        "catho.com.br",
        "indeed.com",
        "jobted.com",
        "glassdoor.com",
        "glassdoor.com.br",
        "bebee.com",
        "trabalhabrasil.com.br",
        "vagas.com.br",
        "solutudo.com.br",
        "telelistas.net",
        "encontrabrasil.com.br",
        "eguias.net",
        "applocal.com.br",
        "viatapida.com",
        "comerciosaopaulo.com.br",
        "mestres.app",
        "mestresdosite.com.br",
        "withnocode.io",
        "babylovegrowth.ai",
        "juriscorrespondente.com.br",
        "webdesignbrasil.org",
        "cnpja.com",
        "cnpj.biz",
        "serasaexperian.com.br",
        "serasa.com.br",
        "radiosaovivo.net",
        "tudoradio.com",
        "msn.com",
        "glassdoor.com",
    }
)

# sites que não são campanha/escritório (mesmo com .br)
JUNK_WEB_MARKERS: tuple[str, ...] = (
    "nameberry.com",
    "behindthename.com",
    "dailymotion.com",
    "github.com",
    "gitlab.com",
    "altavista.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "wikipedia.org",
    "wikimedia.org",
    "outlook.live.com",
    "letras.mus.br",
    "letras.com",
    "cnpja.com",
    "cnpj.biz",
    "serasaexperian",
    "econodata.com",
    "password/reset",
    "oauthredirect",
    "g1.globo.com",
    "nameberry",
    "indeed.com",
    "infojobs.com.br",
    "catho.com.br",
    "glassdoor.com",
    "econodata.com",
    "juridicocerto.com",
    "previdenciarista.com",
    "lawzana.com",
    "ohub.com.br",
    "canaldoanuncio.com",
    "contaazul.com",
    "mestres.app",
    "withnocode.io",
    "babylovegrowth.ai",
    "webdesignbrasil.org",
    "cnpja.com",
    "cnpj.biz",
)

_LISTICLE_PATH_RE = re.compile(
    r"(?ix)"
    r"("
    r"melhores[-/]"
    r"|/melhores/"
    r"|top-10"
    r"|top_10"
    r"|/vagas"
    r"|/jobs?/"
    r"|/q-"
    r"|profissionais/"
    r"|maiores-empresas"
    r"|encontre-contador"
    r"|cidades-atendidas"
    r"|/cidades/"
    r"|hubplace/"
    r"|/listings?/"
    r"|diretorio-de"
    r"|diret[oó]rio-de"
    r"|50-maiores"
    r"|10-melhores"
    r")"
)

_JUNK_NAME_RE = re.compile(
    r"(?ix)"
    r"("
    r"^(melhores|as\s+melhores|os\s+melhores|top\s+\d+|10\s+melhores|"
    r"50\s+maiores|vagas\s+(de|para|em)|vagas\s+de\s+emprego|"
    r"shop\s+)"
    r"|vagas\s+de\s+"
    r"|vagas\s+para\s+"
    r"|vagas\s+de\s+emprego"
    r"|melhores\s+(ag[eê]ncias|empresas|escrit[oó]rios|contadores)"
    r"|maiores\s+empresas"
    r"|top\s+\d+\s+melhores"
    r"|diret[oó]rio\s+de\s+advogados"
    r"|advogados\s+previdenci"
    r"|correspondentes\s+jur[ií]dicos"
    r"|encontra\s+brasil"
    r"|guia\s+telef"
    r")"
)

_CITY_ONLY_NAME_RE = re.compile(
    r"^[A-Za-zÀ-ÿ\s]{2,40},\s*[A-Z]{2}$"
)

NICHE_HINTS: dict[str, tuple[str, ...]] = {
    "advogado": (
        "advog",
        "advocacia",
        "direito",
        "jurid",
        "oab",
        ".adv.br",
        "escritorio",
        "sociedade",
    ),
    "agencia_marketing": (
        "agenc",
        "marketing",
        "publicidade",
        "propaganda",
        "digital",
        "comunicac",
        "midia",
        "mídia",
        "branding",
        "performance",
        "social media",
        "conteudo",
        "conteúdo",
    ),
    "empresa_ti": (
        "software",
        "tecnolog",
        "sistemas",
        "desenvolv",
        "informatica",
        "informática",
        "saas",
        "fábrica",
        "fabrica",
        "house",
        "tech",
        "dados",
        "cloud",
        "ti ",
        " ti",
        "app",
        "programa",
    ),
    "prestador_servico": (
        "contab",
        "contador",
        "assessoria",
        "consultoria",
        "bpo",
        "escritorio",
        "escritório",
        "empresa",
    ),
    "grupo_midiatico": (
        "jornal",
        "radio",
        "rádio",
        "tv",
        "televis",
        "midia",
        "mídia",
        "noticia",
        "notícia",
        "portal",
        "fm",
        "am",
        "grupo",
        "imprensa",
        "redacao",
        "redação",
    ),
}

_PERSON_SEGMENTS = frozenset(
    {"politico", "partido", "advogado", "advogados"}
)

_GENERIC_LOCALS = frozenset(
    {
        "contato",
        "contact",
        "comercial",
        "vendas",
        "hello",
        "ola",
        "info",
        "adm",
        "admin",
        "atendimento",
        "imprensa",
        "comunicacao",
        "campanha",
        "equipe",
        "secretaria",
        "diretorio",
        "assessoria",
        "gabinete",
        "ouvidoria",
        "faleconosco",
        "mail",
        "email",
        "contabilidade",
        "tesouraria",
        "presidencia",
        "formacao",
        "financeiro",
        "imprensa",
    }
)

_NAME_STOP = frozenset(
    {
        "de",
        "da",
        "do",
        "dos",
        "das",
        "e",
        "di",
        "du",
        "del",
        "van",
        "von",
        "campanha",
        "vereador",
        "vereadora",
        "prefeito",
        "prefeita",
        "candidato",
        "candidata",
        "deputado",
        "deputada",
        "senador",
        "senadora",
        "dr",
        "dra",
        "sr",
        "sra",
    }
)

_POLITICO_SIGNAL_RE = re.compile(
    r"candidato|campanha|vereador|prefeito|elei[cç][aã]o|"
    r"partido|diret[oó]rio|comit[eê]|comiss[aã]o\s+provis",
    re.IGNORECASE,
)


def fold(text: str) -> str:
    nk = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nk if not unicodedata.combining(c)).lower()


def person_tokens(name: str) -> list[str]:
    """Tokens úteis do nome (sem cargo/stopword)."""
    raw = fold(name)
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"[—–\-|,/]", " ", raw)
    parts = re.findall(r"[a-z0-9]+", raw)
    return [p for p in parts if p and p not in _NAME_STOP]


def distinctive_tokens(name: str) -> list[str]:
    return [t for t in person_tokens(name) if len(t) >= 4]


def _host(url_or_host: str) -> str:
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "@" in raw and "://" not in raw and not raw.startswith("www."):
        # e-mail passado por engano
        raw = raw.rsplit("@", 1)[-1]
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except Exception:
        host = ""
    return host.removeprefix("www.")


def is_br_domain(domain: str) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return d.endswith(".br")


def is_foreign_cctld(domain_or_host: str) -> bool:
    """True se o host/domínio é claramente estrangeiro (não .br)."""
    host = _host(domain_or_host) or (domain_or_host or "").lower().removeprefix("www.")
    if not host or host.endswith(".br"):
        return False
    for suf in _FOREIGN_SUFFIXES:
        if host.endswith(suf):
            return True
    return False


def is_junk_email_domain(domain: str) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    if d in JUNK_EMAIL_DOMAINS:
        return True
    if is_directory_host(d):
        return True
    return any(d == j or d.endswith("." + j) for j in JUNK_EMAIL_DOMAINS)


def is_directory_host(url_or_host: str) -> bool:
    host = _host(url_or_host) or (url_or_host or "").lower().removeprefix("www.")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def is_listicle_url(url: str) -> bool:
    raw = (url or "").lower()
    if not raw:
        return False
    return bool(_LISTICLE_PATH_RE.search(raw))


def is_junk_web_host(url: str) -> bool:
    raw = (url or "").lower()
    host = _host(url)
    if not host:
        return True
    if is_directory_host(host):
        return True
    if is_listicle_url(raw):
        return True
    for marker in JUNK_WEB_MARKERS:
        if marker in raw or marker in host:
            return True
    return False


def is_plausible_br_website(url: str) -> bool:
    """Site que pode ser o da campanha/empresa (não dicionário, rede social, .jp…)."""
    if not (url or "").strip():
        return False
    if is_junk_web_host(url):
        return False
    host = _host(url)
    if not host:
        return False
    if is_foreign_cctld(host):
        return False
    return True


def is_junk_lead_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if _CITY_ONLY_NAME_RE.match(n):
        return True
    if _JUNK_NAME_RE.search(n):
        return True
    return False


def niche_has_hint(name: str, website: str, snippet: str, segment: str) -> bool:
    hints = NICHE_HINTS.get((segment or "").lower())
    if not hints:
        return True
    blob = fold(f"{name} {snippet} {website}")
    return any(h in blob for h in hints)


def _sld(domain_or_url: str) -> str:
    reg = extract_registrable_domain(domain_or_url or "")
    return fold(reg.split(".")[0]) if reg else ""


def brand_domains_related(a: str, b: str) -> bool:
    """niloalmeida.adv.br ≈ niloalmeidaadvogados.com"""
    sa, sb = _sld(a), _sld(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    if len(sa) >= 6 and len(sb) >= 6 and (sa in sb or sb in sa):
        return True
    return False


def is_plausible_lead(
    *,
    name: str,
    website: str = "",
    email: str = "",
    snippet: str = "",
    segment: str = "",
) -> bool:
    """Empresa/lead que pode entrar no pipeline (todos os nichos)."""
    seg = (segment or "").lower()
    # generalista: só “é um negócio” + “é do Brasil”
    if seg == "generalista":
        return _looks_like_br_business(
            name=name, website=website, email=email, snippet=snippet
        )
    if is_junk_lead_name(name):
        return False
    if website:
        if is_directory_host(website) or is_listicle_url(website):
            return False
        if is_foreign_cctld(website):
            return False
        host = _host(website)
        if host:
            for marker in JUNK_WEB_MARKERS:
                if marker in website.lower() or marker in host:
                    return False
    seg = (segment or "").lower()
    if seg in {"politico", "partido"}:
        from app.providers.public_org import is_politico_target

        return is_politico_target(
            name=name, website=website, email=email, snippet=snippet
        )
    from app.providers.public_org import is_public_organ

    if is_public_organ(
        name=name, website=website, email=email, snippet=snippet, segment=seg
    ):
        return False
    # .com estrangeiro / sem sinal do nicho (pets, vagas, listicle sem keyword)
    host = _host(website)
    if host.endswith("linkedin.com") and seg in {"agencia_marketing", "empresa_ti"}:
        return True
    if seg in NICHE_HINTS and not niche_has_hint(name, website, snippet, seg):
        if not host or not is_br_domain(host):
            return False
    return True


def _looks_like_br_business(
    *,
    name: str,
    website: str = "",
    email: str = "",
    snippet: str = "",
) -> bool:
    """Barra mínima da rotina generalista: negócio (não vaga/listicle/órgão)
    e não estrangeiro. Sem exigir nicho, domínio próprio nem marca."""
    if not (name or "").strip():
        return False
    if is_junk_lead_name(name):
        return False
    from app.providers.public_org import is_public_organ

    if is_public_organ(
        name=name,
        website=website,
        email=email,
        snippet=snippet,
        segment="generalista",
        allow_gov_br=True,
    ):
        return False
    if website:
        if is_foreign_cctld(website):
            return False
        if is_directory_host(website) or is_listicle_url(website):
            return False
        host = _host(website)
        # só recusa hosts que não são empresa (wiki, vagas, gov já cai em público)
        for marker in (
            "wikipedia.org",
            "wikimedia.org",
            "indeed.com",
            "infojobs.com.br",
            "catho.com.br",
            "glassdoor.com",
            "nameberry.com",
        ):
            if marker in (website or "").lower() or (host and marker in host):
                return False
    return True


def local_matches_person(local: str, name: str) -> bool:
    if not local or not name:
        return False
    compact = re.sub(r"[^a-z0-9]", "", fold(local))
    tokens = distinctive_tokens(name)
    if not tokens:
        shorts = person_tokens(name)
        return bool(shorts) and shorts[0] in compact
    return any(t in compact for t in tokens)


def domain_matches_person(domain: str, name: str, *, party: str = "") -> bool:
    """SLD do domínio bate com sobrenome / nome composto / sigla do partido."""
    registrable = extract_registrable_domain(domain) if domain else ""
    sld = registrable.split(".")[0] if registrable else ""
    if not sld:
        return False
    sld_f = fold(sld)

    party_f = fold(party).replace(" ", "")
    if party_f and len(party_f) >= 2 and (
        party_f == sld_f or sld_f.startswith(party_f) or party_f in sld_f
    ):
        return True

    tokens = distinctive_tokens(name)
    if not tokens:
        return False
    if sld_f in tokens:
        return True
    sld_parts = re.findall(r"[a-z0-9]+", sld_f)
    if any(p in tokens for p in sld_parts if len(p) >= 4):
        return True

    for i, a in enumerate(tokens):
        for b in tokens[i + 1 :]:
            if a + b in sld_f or b + a in sld_f:
                return True

    # sobrenome (5+ letras): igual, prefixo ou sufixo — NÃO substring frouxa
    # (evita "alan" ⊂ "alanchikinchow")
    for ln in (t for t in tokens if len(t) >= 5):
        if sld_f == ln or sld_f.startswith(ln) or sld_f.endswith(ln):
            return True
        if f"-{ln}" in sld_f or f"{ln}-" in sld_f:
            return True
    return False


def text_mentions_person(text: str, name: str) -> bool:
    blob = fold(text)
    blob_w = " " + re.sub(r"[^a-z0-9]+", " ", blob) + " "
    tokens = distinctive_tokens(name) or person_tokens(name)
    if not tokens:
        return False
    return any(f" {t} " in blob_w for t in tokens)


def serp_result_relevant_to_person(
    title: str,
    snippet: str,
    link: str,
    *,
    name: str,
    city: str = "",
    state: str = "",
    party: str = "",
) -> bool:
    """Resultado SERP só conta se fala da pessoa E tem sinal de campanha/local."""
    blob = f"{title} {snippet} {link}"
    if not name or not text_mentions_person(blob, name):
        return False
    blob_f = fold(blob)
    if city and fold(city) in blob_f:
        return True
    if party and len(party.strip()) >= 2 and fold(party) in blob_f:
        return True
    if _POLITICO_SIGNAL_RE.search(blob_f):
        return True
    st = fold(state).strip()
    if len(st) >= 2 and re.search(rf"\b{re.escape(st)}\b", blob_f):
        return True
    return False


def classify_contact_email(
    email: str,
    *,
    name: str = "",
    city: str = "",
    party: str = "",
    website: str = "",
    segment: str = "",
) -> tuple[bool, str]:
    """Decide se o e-mail serve como contato de um lead BR.

    Sempre rejeita sintaxe ruim, lixo, diretório e TLD estrangeiro.
    Em nicho de pessoa (político/advogado) exige vínculo com o nome.
    Nos demais nichos o e-mail tem que ser do site/marca, *.br ou free-mail.
    """
    del city  # reservado p/ regras futuras
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr:
        return False, "sintaxe"
    local, _, domain = addr.partition("@")
    domain = domain.removeprefix("www.")
    if not local or not domain or "." not in domain:
        return False, "sintaxe"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False, "sintaxe"
    if is_junk_email_domain(domain) or is_directory_host(domain):
        return False, "dominio_lixo"
    if is_foreign_cctld(domain):
        return False, "tld_estrangeiro"

    from app.providers.public_org import is_gov_br_email, is_public_email

    seg = (segment or "").lower()
    if is_public_email(addr, allow_gov_br=(seg == "generalista")):
        return False, "orgao_publico"

    # generalista: e-mail válido e não estrangeiro basta (gmail, .com, .br, .gov/.leg/.jus.br)
    if seg == "generalista":
        if name and is_junk_lead_name(name):
            return False, "nao_e_negocio"
        return True, "ok_generalista"

    if website and (is_directory_host(website) or is_listicle_url(website)):
        return False, "site_diretorio"
    if name and is_junk_lead_name(name):
        return False, "nome_lixo"

    person_mode = seg in _PERSON_SEGMENTS
    br = is_br_domain(domain)
    free = is_free_mail(addr) or domain in FREE_MAIL
    local_hit = local_matches_person(local, name)
    domain_hit = domain_matches_person(domain, name, party=party)
    generic = local.split(".", 1)[0] in _GENERIC_LOCALS or local in _GENERIC_LOCALS
    site_dom = extract_registrable_domain(website) if website else ""
    on_site = bool(site_dom) and matches_company_domain(addr, site_dom)
    brand_ok = bool(site_dom) and brand_domains_related(site_dom, domain)

    if person_mode:
        if on_site or brand_ok:
            return True, "ok_site"
        # gmail/hotmail/outlook/.com.br de portal — NÃO tratar como domínio próprio
        if free:
            if local_hit:
                return True, "ok_freemail"
            return False, "freemail_sem_nome"
        if br:
            if local_hit or generic or domain_hit:
                return True, "ok_br"
            if domain.endswith(".org.br"):
                return True, "ok_org_br"
            return False, "br_sem_nome"
        if domain_hit and (local_hit or generic):
            return True, "ok_dominio_nome"
        return False, "sem_vinculo_br"

    # empresa / agência / TI / mídia / prestador
    if site_dom and not is_directory_host(site_dom) and not is_listicle_url(website):
        if on_site or free or brand_ok or domain_hit:
            return True, "ok_site"
        return False, "email_fora_do_site"
    if br or free:
        return True, "ok_br_ou_free"
    if domain_hit or brand_ok:
        return True, "ok_marca"
    return False, "sem_vinculo_br"


def judge_lead(
    *,
    name: str,
    email: str = "",
    website: str = "",
    city: str = "",
    segment: str = "",
    snippet: str = "",
    party: str = "",
    contact_name: str = "",
) -> tuple[bool, list[str]]:
    """Veredito único p/ pipeline e p/ rotina de correção.

    keep=False se o lead não encaixa no nicho/nacionalidade BR
    ou se o e-mail é de diretório / estrangeiro / sem vínculo.
    Sem e-mail: só reprova se a empresa em si for lixo.
    """
    reasons: list[str] = []
    if not is_plausible_lead(
        name=name,
        website=website,
        email=email,
        snippet=snippet,
        segment=segment,
    ):
        reasons.append("fora_do_nicho_ou_nacionalidade")
    if email:
        ok, reason = classify_contact_email(
            email,
            name=contact_name or name,
            city=city,
            party=party,
            website=website,
            segment=segment,
        )
        if not ok:
            reasons.append(f"email:{reason}")
    return (not reasons), reasons


_GENERIC_GTLDS = frozenset({"com", "net", "org"})


def email_needs_llm_review(email: str) -> bool:
    """True se o e-mail é gratuito (Gmail, Hotmail, Outlook, Yahoo…)
    ou gTLD genérico (.com/.net/.org sem .br).

    Nesses casos o Qwen local decide se o endereço é do negócio BR.
    """
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[-1].removeprefix("www.")
    if is_free_mail(addr) or domain in FREE_MAIL:
        return True
    if domain.endswith(".br"):
        return False
    tld = domain.rsplit(".", 1)[-1]
    return tld in _GENERIC_GTLDS


def email_is_plausible_br(
    email: str,
    *,
    name: str = "",
    city: str = "",
    party: str = "",
    website: str = "",
    segment: str = "",
) -> bool:
    ok, _reason = classify_contact_email(
        email,
        name=name,
        city=city,
        party=party,
        website=website,
        segment=segment,
    )
    return ok
