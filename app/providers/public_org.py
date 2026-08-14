"""Filtros de órgãos públicos, estatais e instituições que NÃO são alvo comercial.

Evita prospecção acidental para Assembleias, Câmaras, Prefeituras, MP, TJ,
universidades públicas, estatais, etc. — especialmente crítico no nicho
politico (campanha/partido) e em advogados (defensoria, promotoria, OAB
institucional).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Sufixos / hosts típicos de poder público e justiça
PUBLIC_HOST_MARKERS: tuple[str, ...] = (
    ".gov.br",
    ".leg.br",
    ".jus.br",
    ".mp.br",
    ".def.br",
    ".mil.br",
    "planalto.gov.br",
    "brasil.gov.br",
    "gov.br",
    # portais legislativos estaduais comuns (mesmo fora de .leg.br)
    "al.rs.gov.br",
    "al.sp.gov.br",
    "alerj.rj.gov.br",
    "almg.gov.br",
    "al.ba.gov.br",
    "al.ce.gov.br",
    "al.pe.gov.br",
    "al.pr.gov.br",
    "al.sc.gov.br",
    "al.go.gov.br",
    "al.mt.gov.br",
    "al.ms.gov.br",
    "al.es.gov.br",
    "camara.leg.br",
    "senado.leg.br",
    "tse.jus.br",
    "stf.jus.br",
    "stj.jus.br",
    "trf",
    "tre-",
    "tj.",
)

# Domínios de e-mail institucionais (local@domínio)
PUBLIC_EMAIL_DOMAIN_MARKERS: tuple[str, ...] = (
    "gov.br",
    "leg.br",
    "jus.br",
    "mp.br",
    "def.br",
    "mil.br",
    "camara.leg.br",
    "senado.leg.br",
    "tse.jus.br",
)

# Nome / título / snippet que denunciam órgão público
_PUBLIC_NAME_RE = re.compile(
    r"(?ix)"
    r"("
    r"assembl[eé]ia\s+legislativ"
    r"|c[aâ]mara\s+(municipal|dos\s+deputados|de\s+vereadores)"
    r"|senado\s+federal"
    r"|congresso\s+nacional"
    r"|prefeitura(\s+municipal)?"
    r"|governo\s+(do\s+estado|federal|municipal|do\s+)"
    r"|minist[eé]rio\s+(da|do|de|p[uú]blico)"
    r"|secretaria\s+(municipal|estadual|de\s+estado|da|do)"
    r"|defensoria\s+p[uú]blica"
    r"|minist[eé]rio\s+p[uú]blico"
    r"|promotoria"
    r"|procuradoria(\s+geral)?"
    r"|tribunal\s+(de\s+justi[cç]a|regional|superior|de\s+contas)"
    r"|poder\s+judici[aá]rio"
    r"|empresa\s+p[uú]blica"
    r"|sociedade\s+de\s+economia\s+mista"
    r"|autarquia"
    r"|fund[aã]o\s+p[uú]blica"
    r"|instituto\s+federal"
    r"|universidade\s+federal"
    r"|universidade\s+estadual"
    r"|companhia\s+(estadual|municipal|de\s+saneamento|el[eé]trica)"
    r"|\bCEEE\b|\bCORSAN\b|\bSABESP\b|\bCEDAE\b|\bCOPEL\b|\bCELESC\b"
    r"|\bCODEVASF\b|\bEMBRAPA\b|\bIBGE\b|\bINSS\b|\bReceita\s+Federal\b"
    r"|portal\s+da\s+(transpar[eê]ncia|c[aâ]mara|assembl)"
    r"|di[aá]rio\s+oficial"
    r")"
)

# No nicho advogado: instituições jurídicas públicas / conselho (não escritório)
_LAWYER_PUBLIC_RE = re.compile(
    r"(?ix)"
    r"("
    r"defensoria"
    r"|minist[eé]rio\s+p[uú]blico"
    r"|promotoria"
    r"|procuradoria"
    r"|tribunal\s+de\s+justi[cç]a"
    r"|varas?\s+(c[ií]veis?|criminais?|da\s+fazenda)"
    r"|f[oó]rum\s+(c[ií]vel|criminal|da\s+comarca)"
    r"|oab\s+(seccional|nacional|conselho|ordem)"
    r"|ordem\s+dos\s+advogados"
    r"|escola\s+superior\s+de\s+advocacia"
    r"|caixa\s+de\s+assist[eê]ncia\s+dos\s+advogados"
    r")"
)

# No nicho politico: queremos partido/campanha/candidato — NÃO a casa legislativa
_POLITICO_OK_RE = re.compile(
    r"(?ix)"
    r"("
    r"partido"
    r"|diret[oó]rio"
    r"|comiss[aã]o\s+provis[oó]ria"
    r"|campanha"
    r"|candidato"
    r"|coliga[cç][aã]o"
    r"|federa[cç][aã]o\s+partid[aá]ria"
    r"|comit[eê]"
    r"|equipe\s+de\s+campanha"
    r"|\bPT\b|\bPL\b|\bMDB\b|\bPSD\b|\bPP\b|\bUNI[AÃ]O\b|\bPSDB\b|\bPSB\b|\bPDT\b|\bREPUBLICANOS\b|\bPODE(?:mos)?\b|\bCIDADANIA\b|\bPV\b|\bPCdoB\b|\bPSOL\b|\bNOVO\b|\bPRD\b|\bAGIR\b|\bSOLIDARIEDADE\b|\bAVANTE\b|\bDC\b|\bMOBILIZA\b|\bUP\b"
    r")"
)


def _host_from_url(url: str) -> str:
    if not url:
        return ""
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except Exception:
        host = raw.lower()
    return host.removeprefix("www.")


def is_public_host(url_or_host: str) -> bool:
    host = _host_from_url(url_or_host)
    if not host:
        return False
    # *.gov.br / *.leg.br / *.jus.br etc.
    if host.endswith(".gov.br") or host.endswith(".leg.br") or host.endswith(".jus.br"):
        return True
    if host.endswith(".mp.br") or host.endswith(".def.br") or host.endswith(".mil.br"):
        return True
    for marker in PUBLIC_HOST_MARKERS:
        if marker in host:
            return True
    return False


_PUBLIC_BR_QUOTE_TLDS = (".gov.br", ".leg.br", ".jus.br")


def _public_br_quote_suffix(host_or_domain: str) -> bool:
    d = (host_or_domain or "").lower().removeprefix("www.")
    return any(d == tld.lstrip(".") or d.endswith(tld) for tld in _PUBLIC_BR_QUOTE_TLDS)


def is_gov_br_email(email: str) -> bool:
    """True para .gov.br / .leg.br / .jus.br (órgão que pode cotar no generalista)."""
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    return _public_br_quote_suffix(addr.rsplit("@", 1)[-1])


def is_gov_br_host(url_or_host: str) -> bool:
    host = _host_from_url(url_or_host)
    return bool(host) and _public_br_quote_suffix(host)


def is_public_email(email: str, *, allow_gov_br: bool = False) -> bool:
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[-1]
    if _public_br_quote_suffix(domain):
        return not allow_gov_br
    if domain.endswith(".mp.br") or domain.endswith(".def.br") or domain.endswith(".mil.br"):
        return True
    for marker in PUBLIC_EMAIL_DOMAIN_MARKERS:
        if domain == marker or domain.endswith("." + marker) or marker in domain:
            return True
    return False


def name_looks_public(text: str) -> bool:
    return bool(_PUBLIC_NAME_RE.search(text or ""))


def is_public_organ(
    *,
    name: str = "",
    website: str = "",
    email: str = "",
    snippet: str = "",
    segment: str = "",
    allow_gov_br: bool = False,
) -> bool:
    """True se o candidato é órgão público / estatal / instituição indesejada."""
    if (segment or "").lower() == "generalista":
        allow_gov_br = True
    # generalista: prefeitura / câmara / tribunal pode cotar serviço
    if allow_gov_br and (is_gov_br_email(email) or is_gov_br_host(website)):
        return False
    blob = f"{name} {snippet}".strip()
    if is_public_host(website):
        return True
    if email and is_public_email(email, allow_gov_br=allow_gov_br):
        return True
    if name_looks_public(blob):
        if allow_gov_br:
            return False
        return True

    seg = (segment or "").lower()
    if seg in {"advogado", "advogados"}:
        if _LAWYER_PUBLIC_RE.search(blob):
            return True
    return False


def is_politico_target(
    *,
    name: str = "",
    website: str = "",
    email: str = "",
    snippet: str = "",
) -> bool:
    """Para nicho politico: aceita só partido/campanha/candidato privados.

    Rejeita assembleias, câmaras, prefeituras e qualquer .gov/.leg/.jus.
    """
    if is_public_organ(name=name, website=website, email=email, snippet=snippet, segment="politico"):
        return False
    if website:
        from app.providers.geo_email import is_foreign_cctld, is_junk_web_host

        if is_junk_web_host(website) or is_foreign_cctld(website):
            return False
    blob = f"{name} {snippet} {website}".strip()
    # precisa parecer campanha/partido (evita blogs e lixo genérico)
    if _POLITICO_OK_RE.search(blob):
        return True
    # site de partido conhecido (domínio com sigla + org.br sem gov)
    host = _host_from_url(website)
    if host and not is_public_host(host):
        if any(
            x in host
            for x in (
                "partido",
                "diretório",
                "diretorio",
                "campanha",
                ".org.br",
                "elei",
            )
        ):
            return True
    return False


# Partidos com presença nacional (busca de diretórios / sites oficiais)
PARTIDOS_BR: list[dict[str, str]] = [
    {"sigla": "PT", "nome": "Partido dos Trabalhadores", "hint": "pt.org.br"},
    {"sigla": "PL", "nome": "Partido Liberal", "hint": "partidoliberal.org.br"},
    {"sigla": "MDB", "nome": "Movimento Democrático Brasileiro", "hint": "mdb.org.br"},
    {"sigla": "PSD", "nome": "PSD", "hint": "psd.org.br"},
    {"sigla": "PP", "nome": "Progressistas", "hint": "progressistas.org.br"},
    {"sigla": "UNIÃO", "nome": "União Brasil", "hint": "uniaobrasil.org.br"},
    {"sigla": "PSDB", "nome": "PSDB", "hint": "psdb.org.br"},
    {"sigla": "Republicanos", "nome": "Republicanos", "hint": "republicanos10.org.br"},
    {"sigla": "PDT", "nome": "PDT", "hint": "pdt.org.br"},
    {"sigla": "PSB", "nome": "PSB", "hint": "psb.org.br"},
    {"sigla": "Podemos", "nome": "Podemos", "hint": "podemos.org.br"},
    {"sigla": "PSOL", "nome": "PSOL", "hint": "psol50.org.br"},
    {"sigla": "PCdoB", "nome": "PCdoB", "hint": "pcdob.org.br"},
    {"sigla": "NOVO", "nome": "Partido Novo", "hint": "novo.org.br"},
    {"sigla": "Cidadania", "nome": "Cidadania", "hint": "cidadania23.org.br"},
    {"sigla": "PV", "nome": "Partido Verde", "hint": "pv.org.br"},
    {"sigla": "Solidariedade", "nome": "Solidariedade", "hint": "solidariedade.org.br"},
    {"sigla": "Avante", "nome": "Avante", "hint": "avante70.org.br"},
    {"sigla": "PRD", "nome": "PRD", "hint": "prd.org.br"},
]


def negative_search_tokens_public() -> str:
    """Tokens negativos para queries SERP (quando o backend suportar -termo)."""
    return (
        '-site:gov.br -site:leg.br -site:jus.br -site:mp.br '
        '-"assembleia legislativa" -prefeitura -"camara municipal" '
        '-"ministério público" -defensoria -"tribunal de justiça"'
    )
