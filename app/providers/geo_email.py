"""Validação geográfica / de pertinência de e-mail e site.

O prospector é BR. A checagem DNS (MX existe) NÃO basta: um e-mail
americano ou japonês com MX válido passava e era associado a candidato
TSE só porque o nome colidia no SERP (ex.: ALTAVISTA → laramie1.org,
ALAN MONTORO → alanchikinchow.com).

Regras:
  - ccTLD estrangeiro / .edu/.gov/.mil sem .br → rejeita
  - veículo/marca estrangeira conhecida (.com tipo foxnews/cnn) → rejeita
  - HTML do site sem sinal BR (CNPJ, pt-BR, +55) e com sinal US/UK → rejeita
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

# Veículos / marcas .com que o SERP devolve como se fossem empresa BR.
# .br (cnnbrasil.com.br) passa — é operação local.
_FOREIGN_MEDIA_HOSTS: frozenset[str] = frozenset(
    {
        "foxnews.com",
        "fox.com",
        "foxbusiness.com",
        "fox8live.com",
        "fox26houston.com",
        "fox5ny.com",
        "fox5atlanta.com",
        "cnn.com",
        "bbc.com",
        "bbc.co.uk",
        "bbci.co.uk",
        "nytimes.com",
        "washingtonpost.com",
        "wsj.com",
        "reuters.com",
        "apnews.com",
        "ap.org",
        "bloomberg.com",
        "forbes.com",
        "theguardian.com",
        "thetimes.com",
        "telegraph.co.uk",
        "dailymail.co.uk",
        "nypost.com",
        "usatoday.com",
        "latimes.com",
        "nbcnews.com",
        "nbc.com",
        "cbsnews.com",
        "cbs.com",
        "abcnews.go.com",
        "abc.com",
        "msnbc.com",
        "newsweek.com",
        "time.com",
        "politico.com",
        "thehill.com",
        "axios.com",
        "huffpost.com",
        "cnbc.com",
        "marketwatch.com",
        "businessinsider.com",
        "aljazeera.com",
        "dw.com",
        "france24.com",
        "sky.com",
        "skysports.com",
        "espn.com",
        "ft.com",
        "economist.com",
        "news.com.au",
        "abc.net.au",
        "cbc.ca",
        "globalnews.ca",
        "ctvnews.ca",
        "lefigaro.fr",
        "lemonde.fr",
        "elpais.com",
        "elmundo.es",
        "independent.co.uk",
        "thesun.co.uk",
        "variety.com",
        "tmz.com",
        "people.com",
        # tech / SaaS / marcas globais que o SERP de TI/geral devolve
        "salesforce.com",
        "oracle.com",
        "ibm.com",
        "adobe.com",
        "nvidia.com",
        "intel.com",
        "cisco.com",
        "sap.com",
        "hubspot.com",
        "slack.com",
        "dropbox.com",
        "shopify.com",
        "tesla.com",
        "netflix.com",
        "uber.com",
        "airbnb.com",
        "meta.com",
        "openai.com",
        "anthropic.com",
        "microsoft.com",
        "apple.com",
        "amazon.com",
        "aws.amazon.com",
        "google.com",
        "youtube.com",
    }
)
_FOREIGN_MEDIA_SLDS: frozenset[str] = frozenset(
    {
        "foxnews",
        "foxbusiness",
        "fox8live",
        "nytimes",
        "washingtonpost",
        "reuters",
        "bloomberg",
        "theguardian",
        "dailymail",
        "nbcnews",
        "cbsnews",
        "msnbc",
        "aljazeera",
        "businessinsider",
    }
)

_FOREIGN_NAME_RE = re.compile(
    r"(?ix)"
    r"(?:^|\b)("
    r"fox\s*news|foxnews|fox\s*\d+|fox\s+8|"
    r"\bcnn\b|bbc(\s+news)?|new\s+york\s+times|nytimes|"
    r"washington\s+post|wall\s+street\s+journal|\breuters\b|"
    r"associated\s+press|\bnbc\s*news\b|\bcbs\s*news\b|abc\s+news|\bmsnbc\b|"
    r"usa\s+today|los\s+angeles\s+times|the\s+guardian|"
    r"daily\s+mail|\bbloomberg\b|\bwvue\b"
    r")(?:\b|$)"
)
_FOREIGN_GEO_RE = re.compile(
    r"(?ix)\b("
    r"new\s+orleans|new\s+york|los\s+angeles|chicago|miami|houston|"
    r"dallas|atlanta|san\s+francisco|washington\s+d\.?c\.?|"
    r"united\s+states|\bu\.?s\.?a\.?\b|united\s+kingdom|"
    r"london|manchester|lisboa|lisbon"
    r")\b"
)

_CNPJ_RE = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}\s*[\/]\s*\d{4}-?\d{2}\b")
_CEP_RE = re.compile(r"\bCEP\s*:?\s*\d{5}-?\d{3}\b", re.I)
_PLUS55_RE = re.compile(r"\+55\D{0,3}\d{2}")
_HTML_LANG_RE = re.compile(
    r"<html\b[^>]*\slang\s*=\s*['\"]\s*([a-zA-Z]{2}(?:[-_][a-zA-Z]{2})?)",
    re.I,
)
_META_LOCALE_RE = re.compile(
    r"""content\s*=\s*['"]?(pt[-_]BR|en[-_]US|en[-_]GB)['"]?"""
    r"""|['"](?:og:locale|language)['"][^>]*content\s*=\s*['"]([^'"]+)""",
    re.I,
)
_BR_HTML_PHRASES = (
    "cnpj",
    "cpf",
    "inscrição estadual",
    "inscricao estadual",
    "razão social",
    "razao social",
    "fale conosco",
    "política de privacidade",
    "politica de privacidade",
    "todos os direitos reservados",
)
_FOREIGN_HTML_PHRASES = (
    "united states",
    "u.s.a",
    "new york",
    "new orleans",
    "los angeles",
    "washington, d",
    "fcc public",
    "public file",
    "headquartered in",
)
_FOREIGN_EMAIL_LOCALS = frozenset({"publicfile", "public.file", "fcc"})

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
    r"shop\s+|o que [eé]\b|como funciona|saiba (mais|como)|veja como|"
    r"entenda |resumo sobre|conceitos\b|contagem regressiva)"
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
    r"|wikip[eé]dia"
    r"|defini[cç][aã]o de"
    r"|dicion[aá]rio"
    r"|falta um m[eê]s"
    r"|festa de s[aã]o"
    r"|:\s*(conceitos|impactos|defini|o que)"
    r")"
)
_GENERIC_SOLE_NAMES = frozenset(
    {
        "equipe",
        "wikipedia",
        "sociedade",
        "software",
        "home",
        "blog",
        "contato",
        "noticias",
        "notícias",
        "artigo",
        "links",
        "download",
        "downloads",
    }
)
_CAMPAIGN_NAME_RE = re.compile(
    r"(?ix)"
    r"(^campanha\b)"
    r"|(\bcampanha\s+[A-ZÁÉÍÓÚÂÊÔÃÕÜ])"
    r"|(\(\s*(PT|PL|PSOL|MDB|UNI[AÃ]O|PP|PSDB|PDT|REPUBLICANOS|"
    r"AGIR|PODE|NOVO|PCDOB|PV|CIDADANIA|SOLIDARIEDADE|PRD|DC|PSB)\s*\))"
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

# caixas que nunca são contato comercial (LGPD, NDR, ouvidoria)
_NON_CONTACT_LOCALS = frozenset(
    {
        "privacidade",
        "privacy",
        "lgpd",
        "dpo",
        "dataprotection",
        "noreply",
        "no-reply",
        "donotreply",
        "no_reply",
        "ouvidoria",
        "webmaster",
        "abuse",
        "postmaster",
        "mailer-daemon",
        "mailerdaemon",
    }
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


def site_origin(url: str) -> str:
    """https://www.foxnews.com/politics/foo → https://www.foxnews.com/"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if not parsed.netloc:
        return raw
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/"


def is_known_foreign_host(url_or_host: str) -> bool:
    """Fox News, CNN.com, BBC… — não .br (CNN Brasil passa)."""
    host = _host(url_or_host) or (url_or_host or "").lower().removeprefix("www.")
    if not host or host.endswith(".br"):
        return False
    reg = extract_registrable_domain(host) or host
    for blocked in _FOREIGN_MEDIA_HOSTS:
        if host == blocked or host.endswith("." + blocked) or reg == blocked:
            return True
    sld = reg.split(".")[0] if reg else ""
    return sld in _FOREIGN_MEDIA_SLDS


def _name_claims_brazil(name: str) -> bool:
    n = fold(name)
    if "brasil" in n or "brazil" in n:
        return True
    return bool(
        re.search(r"\b(ltda|eireli|me|epp|s/?a|s\.a\.?)\b", n)
    )


def is_foreign_company(
    *,
    name: str = "",
    website: str = "",
    email: str = "",
    snippet: str = "",
) -> bool:
    """True se o lead é claramente empresa/veículo de outro país.

    snippet não entra no nome — artigo sobre o Brasil cita 'Brasil' e
    'Fox News' no mesmo parágrafo.
    """
    del snippet  # não usar: SERP mistura veículo estrangeiro + pauta BR
    host = _host(website)
    email_dom = ""
    if email and "@" in email:
        email_dom = email.rsplit("@", 1)[-1].lower().removeprefix("www.")
    if is_br_domain(host) or is_br_domain(email_dom):
        return False
    if _name_claims_brazil(name):
        return False
    if is_known_foreign_host(website) or is_known_foreign_host(email_dom):
        return True
    if is_foreign_cctld(website) or is_foreign_cctld(email_dom):
        return True
    n = fold(name)
    if n and _FOREIGN_GEO_RE.search(n):
        return True
    if n and _FOREIGN_NAME_RE.search(n):
        return True
    return False


def keep_brazilian_search_hit(
    *,
    title: str = "",
    link: str = "",
    snippet: str = "",
    email: str = "",
) -> bool:
    """Vale para todo nicho e o generalista: SERP/places/news só se não for gringo."""
    if is_known_foreign_host(link):
        return False
    if is_foreign_company(name=title, website=link, email=email, snippet=snippet):
        return False
    return True


def inspect_html_nationality(html: str) -> tuple[list[str], list[str]]:
    """Sinais BR vs estrangeiro no HTML/texto do site (CNPJ, lang, +55…)."""
    raw = html or ""
    if not raw:
        return [], []
    sample = raw[:80_000]
    low = fold(sample)
    br: list[str] = []
    fo: list[str] = []

    langs: list[str] = []
    m_lang = _HTML_LANG_RE.search(sample)
    if m_lang:
        langs.append(m_lang.group(1).lower().replace("_", "-"))
    for m in _META_LOCALE_RE.finditer(sample):
        loc = (m.group(1) or m.group(2) or "").lower().replace("_", "-")
        if loc:
            langs.append(loc)

    if any(x.startswith("pt") for x in langs):
        br.append("lang_pt")
    if any(x in {"en-us", "en-gb"} or x.startswith("en-") for x in langs):
        fo.append("lang_en")

    if _CNPJ_RE.search(sample) or "cnpj" in low:
        br.append("cnpj")
    if _CEP_RE.search(sample):
        br.append("cep")
    if _PLUS55_RE.search(sample):
        br.append("+55")
    if "r$" in low:
        br.append("brl")
    for phrase in _BR_HTML_PHRASES:
        if fold(phrase) in low:
            br.append(fold(phrase).replace(" ", "_")[:24])
            break

    for phrase in _FOREIGN_HTML_PHRASES:
        if fold(phrase) in low:
            fo.append(fold(phrase).replace(" ", "_")[:24])
            break

    def _uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it not in seen:
                seen.add(it)
                out.append(it)
        return out

    return _uniq(br), _uniq(fo)


_STRONG_BR_SIGNALS = frozenset(
    {
        "lang_pt",
        "cnpj",
        "cep",
        "+55",
        "fale_conosco",
        "razao_social",
        "inscricao_estadual",
        "politica_de_privacidade",
        "todos_os_direitos_reserv",
    }
)


def verdict_from_html_signals(br: list[str], fo: list[str]) -> str:
    """br | foreign | inconclusive.

    R$ / WhatsApp soltos não salvam um site en-US com endereço em NY.
    """
    strong_br = [s for s in br if s in _STRONG_BR_SIGNALS]
    if strong_br:
        return "br"
    if fo:
        return "foreign"
    if br:
        return "br"
    return "inconclusive"


def html_says_foreign(html: str) -> bool:
    """True só quando o HTML aponta outro país e nenhum sinal BR forte."""
    br, fo = inspect_html_nationality(html)
    return verdict_from_html_signals(br, fo) == "foreign"


def html_says_brazilian(html: str) -> bool:
    br, fo = inspect_html_nationality(html)
    return verdict_from_html_signals(br, fo) == "br"


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
    if is_foreign_cctld(host) or is_known_foreign_host(url):
        return False
    return True


def is_junk_lead_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if fold(n) in _GENERIC_SOLE_NAMES:
        return True
    if ":" in n and len(n) > 40:
        return True
    if _CITY_ONLY_NAME_RE.match(n):
        return True
    if _JUNK_NAME_RE.search(n):
        return True
    return False


def looks_like_campaign_name(name: str, extra: dict | None = None) -> bool:
    """Nome/origem de campanha ou partido — não recebe template generalista."""
    extra = extra or {}
    origin = str(
        extra.get("review_origin_niche")
        or extra.get("origin")
        or extra.get("segment")
        or ""
    ).lower()
    if origin in {"politico", "partido"}:
        return True
    n = (name or "").strip()
    if not n:
        return False
    if _CAMPAIGN_NAME_RE.search(n):
        return True
    folded = fold(n)
    if folded.startswith("campanha"):
        return True
    return bool(re.match(r"(?ix)^(psol|mdb|partido dos|partido liberal|diretorio|diretório)\b", n))


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
    if is_foreign_company(name=name, website=website, email=email, snippet=snippet):
        return False
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
    if is_foreign_company(name=name, website=website, email=email, snippet=snippet):
        return False
    if is_junk_lead_name(name):
        return False
    if looks_like_campaign_name(name):
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
    if is_foreign_cctld(domain) or is_known_foreign_host(domain):
        return False, "tld_estrangeiro"
    if local in _FOREIGN_EMAIL_LOCALS:
        return False, "empresa_estrangeira"
    local_head = local.split(".", 1)[0].split("+", 1)[0].replace("-", "")
    if local in _NON_CONTACT_LOCALS or local_head in _NON_CONTACT_LOCALS:
        return False, "caixa_nao_contato"
    if is_foreign_company(name=name, website=website or domain, email=addr):
        return False, "empresa_estrangeira"

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
