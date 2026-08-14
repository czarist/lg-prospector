"""Candidatos via API pública DivulgaCandContas (TSE).

Lista por município/UF (eleições municipais 2024 por padrão). O TSE **não
publica e-mail** na listagem — este módulo só entrega nomes/partido/cargo;
o provider de políticos busca e-mail na web e **só aceita se achar**.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from app.core.logging import get_logger
from app.infrastructure.resilience.retry import async_retry

logger = get_logger(__name__)

BASE = "https://divulgacandcontas.tse.jus.br/divulga/rest/v1"
# Eleições municipais 2024 (prefeito/vereador)
DEFAULT_YEAR = 2024
DEFAULT_ELECTION_ID = 2045202024
# 11=Prefeito, 12=Vice, 13=Vereador
DEFAULT_CARGOS = (11, 13)

def _cache_dir() -> Path:
    from app.core.paths import logs_dir

    return logs_dir() / "cache" / "tse"


_HEADERS = {
    "User-Agent": "LG-Prospector/0.1 (prospeccao-b2b; +https://local)",
    "Accept": "application/json",
    "Referer": "https://divulgacandcontas.tse.jus.br/divulga/",
}


def _strip_accents(s: str) -> str:
    nk = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nk if not unicodedata.combining(c))


def _norm_name(s: str) -> str:
    s = _strip_accents(s).upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # remove sufixos comuns de município
    for bad in (" - RS", " - SP", " / RS", " / SP"):
        s = s.replace(bad, "")
    return s


@dataclass
class TseCandidate:
    id: str
    nome_urna: str
    nome_completo: str
    cargo: str
    cargo_codigo: int
    partido_sigla: str
    numero: str = ""
    situacao: str = ""
    totalizacao: str = ""
    city: str = ""
    state: str = ""
    municipality_code: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        party = self.partido_sigla or "?"
        cargo = self.cargo or "Candidato"
        name = self.nome_urna or self.nome_completo
        return f"Campanha {name} ({party}) — {cargo}"


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    async def _do():
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    return await async_retry(_do, attempts=2, exceptions=(httpx.HTTPError,))


def _cache_path(uf: str) -> Path:
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"municipios_{uf.upper()}_{DEFAULT_ELECTION_ID}.json"


async def list_municipios(
    uf: str,
    *,
    election_id: int = DEFAULT_ELECTION_ID,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """Lista municípios da UF: [{codigo, nome}]. Cache em disco."""
    uf = (uf or "").strip().upper()
    if not uf or len(uf) != 2:
        return []

    path = _cache_path(uf)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    url = f"{BASE}/eleicao/buscar/{uf}/{election_id}/municipios"
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=40.0, headers=_HEADERS, follow_redirects=True)
    assert client is not None
    try:
        data = await _get_json(client, url)
    finally:
        if own:
            await client.aclose()

    munis = data.get("municipios") or []
    out = [
        {
            "codigo": str(m.get("codigo") or m.get("id") or ""),
            "nome": str(m.get("nome") or ""),
        }
        for m in munis
        if m.get("nome") and (m.get("codigo") is not None or m.get("id") is not None)
    ]
    try:
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    logger.info("tse_municipios_loaded", uf=uf, n=len(out))
    return out


async def resolve_municipality_code(
    city: str,
    state: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> Optional[str]:
    munis = await list_municipios(state, client=client)
    target = _norm_name(city)
    if not target:
        return None
    # match exato
    for m in munis:
        if _norm_name(m["nome"]) == target:
            return m["codigo"]
    # match começa com / contém
    for m in munis:
        n = _norm_name(m["nome"])
        if n.startswith(target) or target.startswith(n):
            return m["codigo"]
    for m in munis:
        n = _norm_name(m["nome"])
        if target in n or n in target:
            return m["codigo"]
    logger.warning("tse_municipio_not_found", city=city, state=state)
    return None


def _parse_candidate(raw: dict[str, Any], *, city: str, state: str, mun_code: str) -> TseCandidate | None:
    nome = (raw.get("nomeUrna") or raw.get("nomeCompleto") or "").strip()
    if not nome:
        return None
    cargo = raw.get("cargo") or {}
    partido = raw.get("partido") or {}
    sit = (raw.get("descricaoSituacao") or "").strip()
    # só deferidos / aptos quando possível
    if sit and sit.lower() not in {"deferido", "deferido com recurso", ""}:
        if "indefer" in sit.lower() or "cancel" in sit.lower() or "cassad" in sit.lower():
            return None
    return TseCandidate(
        id=str(raw.get("id") or ""),
        nome_urna=(raw.get("nomeUrna") or nome).strip(),
        nome_completo=(raw.get("nomeCompleto") or nome).strip(),
        cargo=str(cargo.get("nome") or "Candidato"),
        cargo_codigo=int(cargo.get("codigo") or 0),
        partido_sigla=str(partido.get("sigla") or "").strip(),
        numero=str(raw.get("numero") or ""),
        situacao=sit,
        totalizacao=str(raw.get("descricaoTotalizacao") or ""),
        city=city,
        state=state,
        municipality_code=mun_code,
        extra={
            "coligacao": raw.get("nomeColigacao"),
            "cnpj_campanha": raw.get("cnpjcampanha"),
        },
    )


async def fetch_candidates_for_city(
    city: str,
    state: str,
    *,
    max_results: int = 30,
    year: int = DEFAULT_YEAR,
    election_id: int = DEFAULT_ELECTION_ID,
    cargos: tuple[int, ...] = DEFAULT_CARGOS,
) -> list[TseCandidate]:
    """Busca candidatos municipais (prefeito/vereador) na cidade."""
    state = (state or "").strip().upper()
    city = (city or "").strip()
    if not state or not city:
        return []

    async with httpx.AsyncClient(timeout=60.0, headers=_HEADERS, follow_redirects=True) as client:
        mun_code = await resolve_municipality_code(city, state, client=client)
        if not mun_code:
            return []

        out: list[TseCandidate] = []
        seen: set[str] = set()
        for cargo in cargos:
            if len(out) >= max_results:
                break
            url = (
                f"{BASE}/candidatura/listar/{year}/{mun_code}/"
                f"{election_id}/{cargo}/candidatos"
            )
            try:
                data = await _get_json(client, url)
            except Exception as exc:
                logger.warning(
                    "tse_list_failed",
                    cargo=cargo,
                    city=city,
                    state=state,
                    error=str(exc),
                )
                continue
            raw_list = data.get("candidatos") or []
            # prefeitos primeiro (lista pequena); vereadores: limita
            if cargo == 13 and len(raw_list) > max_results * 3:
                # espalha partidos: pega os primeiros N e um sample do meio
                raw_list = raw_list[: max_results * 2]

            for raw in raw_list:
                cand = _parse_candidate(raw, city=city, state=state, mun_code=mun_code)
                if not cand or not cand.id:
                    continue
                if cand.id in seen:
                    continue
                seen.add(cand.id)
                out.append(cand)
                if len(out) >= max_results:
                    break

        logger.info(
            "tse_candidates_fetched",
            city=city,
            state=state,
            mun=mun_code,
            n=len(out),
        )
        return out[:max_results]
