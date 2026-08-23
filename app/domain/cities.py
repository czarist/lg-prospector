"""Escada de cidades comercialmente relevantes (população / PIB / capital).

Padrão da caçada: Brasil inteiro (sem travar município).
A escada cidade a cidade é opt-in (--no-nationwide / --focus-rs / --only-rs).

Fonte: curadoria estática (IBGE pop ~2022–2024 / relevância PIB municipal).
Não baixa API externa — lista estável e barata em token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

NATIONWIDE_LABEL = "Brasil"
_NATIONWIDE_CITIES = frozenset({"brasil", "brazil", "br", "país", "pais"})
_NATIONWIDE_STATES = frozenset({"", "BR", "BRASIL"})


@dataclass(frozen=True, slots=True)
class CityTarget:
    city: str
    state: str  # UF (vazio = nacional)
    tier: int  # 0=país, 1=capital/DF, 2=polo regional, 3=secundário importante
    population_k: int  # mil habitantes (aprox.)
    region: str = ""  # nacional | sul | sudeste | centro-oeste | nordeste | norte
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.city}|{self.state}".lower()

    @property
    def label(self) -> str:
        if not (self.state or "").strip():
            return self.city
        return f"{self.city}/{self.state}"

    @property
    def nationwide(self) -> bool:
        return is_nationwide(self.city, self.state)


def is_nationwide(city: str = "", state: str = "") -> bool:
    """True se a busca é o país inteiro (sem município)."""
    c = (city or "").strip().lower()
    s = (state or "").strip().upper()
    if c in _NATIONWIDE_CITIES:
        return True
    return not c and s in _NATIONWIDE_STATES


def search_location(city: str = "", state: str = "") -> str:
    """Texto de local para query de busca. Vazio / Brasil = 'Brasil'."""
    if is_nationwide(city, state):
        return NATIONWIDE_LABEL
    parts = [(city or "").strip()]
    st = (state or "").strip()
    if st and st.upper() not in _NATIONWIDE_STATES:
        parts.append(st)
    return " ".join(p for p in parts if p)


def nationwide_target() -> CityTarget:
    return CityTarget(NATIONWIDE_LABEL, "", 0, 203000, "nacional", "todo o país")


# ---------------------------------------------------------------------------
# Tier 1 — Capitais + DF (ordem da lista é irrelevante; a fila reordena)
# ---------------------------------------------------------------------------
_CAPITALS: list[CityTarget] = [
    CityTarget("Porto Alegre", "RS", 1, 1333, "sul", "capital RS"),
    CityTarget("Brasília", "DF", 1, 2817, "centro-oeste", "DF / sede federal"),
    CityTarget("São Paulo", "SP", 1, 11450, "sudeste", "maior economia"),
    CityTarget("Rio de Janeiro", "RJ", 1, 6211, "sudeste"),
    CityTarget("Belo Horizonte", "MG", 1, 2315, "sudeste"),
    CityTarget("Curitiba", "PR", 1, 1774, "sul"),
    CityTarget("Florianópolis", "SC", 1, 537, "sul"),
    CityTarget("Salvador", "BA", 1, 2418, "nordeste"),
    CityTarget("Recife", "PE", 1, 1488, "nordeste"),
    CityTarget("Fortaleza", "CE", 1, 2428, "nordeste"),
    CityTarget("Manaus", "AM", 1, 2063, "norte"),
    CityTarget("Belém", "PA", 1, 1303, "norte"),
    CityTarget("Goiânia", "GO", 1, 1437, "centro-oeste"),
    CityTarget("Campo Grande", "MS", 1, 898, "centro-oeste"),
    CityTarget("Cuiabá", "MT", 1, 651, "centro-oeste"),
    CityTarget("Vitória", "ES", 1, 322, "sudeste"),
    CityTarget("Natal", "RN", 1, 751, "nordeste"),
    CityTarget("João Pessoa", "PB", 1, 833, "nordeste"),
    CityTarget("Maceió", "AL", 1, 957, "nordeste"),
    CityTarget("Aracaju", "SE", 1, 603, "nordeste"),
    CityTarget("Teresina", "PI", 1, 866, "nordeste"),
    CityTarget("São Luís", "MA", 1, 1036, "nordeste"),
    CityTarget("Palmas", "TO", 1, 306, "norte"),
    CityTarget("Porto Velho", "RO", 1, 460, "norte"),
    CityTarget("Rio Branco", "AC", 1, 364, "norte"),
    CityTarget("Boa Vista", "RR", 1, 413, "norte"),
    CityTarget("Macapá", "AP", 1, 443, "norte"),
]

# ---------------------------------------------------------------------------
# Tier 2 — Polos RS (após capital gaúcha) + metros nacionais de alto PIB
# ---------------------------------------------------------------------------
_RS_POLOS: list[CityTarget] = [
    CityTarget("Caxias do Sul", "RS", 2, 517, "sul", "indústria / metalmecânica"),
    CityTarget("Canoas", "RS", 2, 348, "sul", "região metropolitana POA"),
    CityTarget("Pelotas", "RS", 2, 326, "sul", "sul do estado"),
    CityTarget("Santa Maria", "RS", 2, 272, "sul", "centro do estado"),
    CityTarget("Gravataí", "RS", 2, 285, "sul", "RM POA / indústria"),
    CityTarget("Novo Hamburgo", "RS", 2, 247, "sul", "couro-calçados / Vale"),
    CityTarget("São Leopoldo", "RS", 2, 238, "sul", "Vale dos Sinos"),
    CityTarget("Passo Fundo", "RS", 2, 206, "sul", "norte gaúcho"),
    CityTarget("Rio Grande", "RS", 2, 191, "sul", "porto"),
    CityTarget("Uruguaiana", "RS", 2, 126, "sul", "fronteira / comércio"),
    CityTarget("Bento Gonçalves", "RS", 2, 123, "sul", "serra / vinho / móveis"),
    CityTarget("Santa Cruz do Sul", "RS", 2, 133, "sul", "tabaco / indústria"),
    CityTarget("Bagé", "RS", 2, 122, "sul", "campanha / fronteira"),
    CityTarget("Alvorada", "RS", 2, 210, "sul", "RM POA"),
    CityTarget("Viamão", "RS", 2, 256, "sul", "RM POA"),
    CityTarget("Cachoeirinha", "RS", 2, 138, "sul", "RM POA"),
    CityTarget("Sapucaia do Sul", "RS", 2, 141, "sul", "RM POA"),
    CityTarget("Lajeado", "RS", 2, 94, "sul", "Vale do Taquari"),
    CityTarget("Erechim", "RS", 2, 106, "sul", "norte"),
]

_NATIONAL_POLOS: list[CityTarget] = [
    # SP interior / ABC
    CityTarget("Campinas", "SP", 2, 1138, "sudeste", "PIB alto"),
    CityTarget("Guarulhos", "SP", 2, 1292, "sudeste"),
    CityTarget("São Bernardo do Campo", "SP", 2, 811, "sudeste"),
    CityTarget("Santo André", "SP", 2, 749, "sudeste"),
    CityTarget("Osasco", "SP", 2, 729, "sudeste"),
    CityTarget("Ribeirão Preto", "SP", 2, 698, "sudeste"),
    CityTarget("Sorocaba", "SP", 2, 724, "sudeste"),
    CityTarget("Santos", "SP", 2, 419, "sudeste", "porto"),
    CityTarget("São José dos Campos", "SP", 2, 697, "sudeste"),
    CityTarget("Jundiaí", "SP", 2, 443, "sudeste"),
    # RJ / MG
    CityTarget("Niterói", "RJ", 2, 516, "sudeste"),
    CityTarget("Duque de Caxias", "RJ", 2, 808, "sudeste"),
    CityTarget("Uberlândia", "MG", 2, 713, "sudeste"),
    CityTarget("Contagem", "MG", 2, 621, "sudeste"),
    # Sul fora RS
    CityTarget("Joinville", "SC", 2, 616, "sul"),
    CityTarget("Blumenau", "SC", 2, 361, "sul"),
    CityTarget("Londrina", "PR", 2, 556, "sul"),
    CityTarget("Maringá", "PR", 2, 430, "sul"),
    # CO / NE relevantes
    CityTarget("Aparecida de Goiânia", "GO", 2, 601, "centro-oeste"),
    CityTarget("Anápolis", "GO", 2, 399, "centro-oeste"),
]

# Tier 3 — só se explicitamente habilitado (ainda comercialmente úteis, menor prioridade)
_TIER3: list[CityTarget] = [
    CityTarget("Santa Rosa", "RS", 3, 74, "sul"),
    CityTarget("Santana do Livramento", "RS", 3, 76, "sul"),
    CityTarget("Ijuí", "RS", 3, 83, "sul"),
    CityTarget("Camaquã", "RS", 3, 66, "sul"),
    CityTarget("Esteio", "RS", 3, 83, "sul"),
    CityTarget("Farroupilha", "RS", 3, 73, "sul"),
    CityTarget("Vacaria", "RS", 3, 67, "sul"),
]


def all_city_targets() -> list[CityTarget]:
    return list(_CAPITALS) + list(_RS_POLOS) + list(_NATIONAL_POLOS) + list(_TIER3)


def build_city_queue(
    *,
    focus_rs: bool = False,
    max_tier: int = 2,
    min_population_k: int = 90,
    only_rs: bool = False,
    only_capitals: bool = False,
    limit: Optional[int] = None,
    include_states: Optional[Iterable[str]] = None,
) -> list[CityTarget]:
    """
    Monta fila de cidades na ordem da 'escadinha':

    1. Capitais (por população; RS primeiro só se focus_rs)
    2. Polos RS + polos nacionais (pop/PIB) — RS polos sempre entram no recorte nacional
    3. Tier 3 só se max_tier>=3

    min_population_k descarta município pequeno (default 90k).
    """
    include_states_set = {s.upper() for s in include_states} if include_states else None

    capitals = list(_CAPITALS)
    if focus_rs:
        capitals = sorted(
            capitals,
            key=lambda c: (
                0 if c.state == "RS" else 1 if c.state == "DF" else 2,
                -c.population_k,
            ),
        )
    else:
        capitals = sorted(capitals, key=lambda c: -c.population_k)

    # polos gaúchos fazem parte do país; só saem se only_capitals
    rs_polos = [] if only_capitals else list(_RS_POLOS)
    national = [] if only_rs or only_capitals else list(_NATIONAL_POLOS)
    rs_polos = sorted(rs_polos, key=lambda c: -c.population_k)
    national = sorted(national, key=lambda c: -c.population_k)

    tier3 = list(_TIER3) if max_tier >= 3 else []
    if only_capitals:
        queue = capitals
    elif focus_rs:
        queue = capitals + rs_polos + national + tier3
    else:
        queue = capitals + sorted(rs_polos + national, key=lambda c: -c.population_k) + tier3

    out: list[CityTarget] = []
    seen: set[str] = set()
    for c in queue:
        if c.tier > max_tier:
            continue
        if c.population_k < min_population_k and c.tier > 1:
            # capitais sempre entram mesmo se pequenas (ex: Vitória, Palmas)
            continue
        if only_rs and c.state != "RS":
            continue
        if include_states_set and c.state not in include_states_set:
            continue
        if c.key in seen:
            continue
        seen.add(c.key)
        out.append(c)

    if limit is not None and limit > 0:
        out = out[:limit]
    return out


def resolve_hunt_cities(
    *,
    nationwide: bool = True,
    focus_rs: bool = False,
    only_rs: bool = False,
    only_capitals: bool = False,
    max_tier: int = 2,
    min_population_k: int = 90,
    limit: Optional[int] = None,
    include_states: Optional[Iterable[str]] = None,
    city: str = "",
    state: str = "",
) -> list[CityTarget]:
    """Fila efetiva da caçada. Padrão: um alvo 'Brasil' (todo o país)."""
    locked = (city or "").strip()
    if locked and not is_nationwide(locked, state):
        return [CityTarget(locked, (state or "").strip(), 1, 0, "")]

    states = list(include_states) if include_states else None
    use_ladder = (
        (not nationwide)
        or focus_rs
        or only_rs
        or only_capitals
        or bool(states)
        or (limit is not None and limit > 0)
    )
    if not use_ladder:
        return [nationwide_target()]
    return build_city_queue(
        focus_rs=focus_rs,
        max_tier=max_tier,
        min_population_k=min_population_k,
        only_rs=only_rs,
        only_capitals=only_capitals,
        limit=limit,
        include_states=states,
    )


# Queries padrão por nicho (rotação)
NICHE_QUERIES: dict[str, list[str]] = {
    "advogado": [
        "escritório advocacia",
        "sociedade de advogados",
        "advogado empresarial",
        "advogados associados",
        "escritório advocacia trabalhista",
        "advogado cível escritório",
        "sociedade de advogados OAB",
        "advogado tributarista escritório",
        "advogado previdenciário escritório",
        "advocacia imobiliária site contato",
    ],
    "agencia_marketing": [
        "agência de marketing digital",
        "agência de publicidade",
        "agência performance google ads",
        "agência de comunicação",
        "agência inbound marketing",
        "agência social media",
        "estúdio branding design",
        "agência SEO site contato",
    ],
    "empresa_ti": [
        "software house",
        "fábrica de software",
        "empresa de desenvolvimento de sistemas",
        "consultoria em ti",
        "empresa de software erp",
        "desenvolvimento de aplicativos",
        "integradora de sistemas",
        "suporte ti terceirizado",
    ],
    "prestador_servico": [
        "escritório de contabilidade",
        "contabilidade empresarial",
        "consultoria empresarial",
        "escritório contábil",
        "assessoria fiscal empresa",
        "consultoria financeira empresa",
        "recursos humanos empresa",
        "engenharia consultoria",
    ],
    "grupo_midiatico": [
        "portal de notícias",
        "jornal regional",
        "grupo de mídia",
        "rádio jornal",
        "tv regional contato comercial",
        "revista regional publicidade",
        "portal de notícias contato comercial",
        "rádio fm comercial",
    ],
    "politico": [
        # NÃO usar "gabinete" / portais .leg.br — puxa assembleia/câmara
        "diretório municipal partido contato",
        "diretório estadual partido contato",
        "comissão provisória partido",
        "candidato campanha site contato",
        "equipe de campanha contato email",
        "partido político diretório site oficial",
        "vereador eleito contato email",
        "pré-candidato prefeito contato email",
        "coordenação de campanha partido email",
        "assessoria de campanha contato",
    ],
    "generalista": [
        "empresa comércio site contato",
        "clínica consultório site",
        "imobiliária site contato",
        "restaurante site institucional",
        "oficina loja indústria site",
        "prestador de serviço empresa site",
        "farmácia drogaria site contato",
        "construtora engenharia site",
        "transportadora logística site",
        "hotel pousada site",
        "contabilidade escritório site",
        "distribuidora atacado site",
    ],
}

# hunt_loop de nicho — sem generalista nem prestador (prestador = faixa geral)
DEFAULT_NICHES: list[str] = [
    k for k in NICHE_QUERIES if k not in {"generalista", "prestador_servico"}
]


def pick_query(niche: str, city: str, round_idx: int = 0) -> str:
    qs = NICHE_QUERIES.get(niche) or [niche.replace("_", " ")]
    base = qs[round_idx % len(qs)]
    loc = search_location(city)
    return f"{base} {loc}".strip()
