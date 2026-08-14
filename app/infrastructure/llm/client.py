"""Cliente LiteLLM (OpenAI-compatible) — uso opcional e serializado.

Caçada funciona sem LLM. Com HUNT_USE_LLM=true, Qwen local (local-main)
filtra candidatos e revisa leads existentes (1 chamada por vez).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.limits import llm_semaphore
from app.core.logging import get_logger

logger = get_logger(__name__)

# Regras por nicho (injetadas no prompt — curtas, estáveis no 7B)
_NICHE_RULES: dict[str, str] = {
    "advogado": (
        "KEEP: escritório de advocacia, sociedade de advogados, firmas jurídicas. "
        "DROP: notícias jurídicas genéricas, OAB institucional genérica, fóruns, "
        "artigos 'o que é direito', diretórios sem firma."
    ),
    "agencia_marketing": (
        "KEEP: agência de marketing digital, publicidade, performance, social media, "
        "comunicação/publicidade. "
        "DROP: agência BANCÁRIA, correios, lotérica, prefeitura, banco, "
        "lista de agências de banco."
    ),
    "empresa_ti": (
        "KEEP: software house, fábrica de software, consultoria TI, SaaS, "
        "desenvolvimento de sistemas, empresa de tecnologia. "
        "DROP: wikipedia, dicionário, blog 'o que é software', download Microsoft/ASUS, "
        "marketplace genérico, curso online genérico."
    ),
    "prestador_servico": (
        "KEEP: contabilidade, consultoria empresarial, serviços B2B locais. "
        "DROP: artigos, órgãos públicos genéricos, marketplaces."
    ),
    "grupo_midiatico": (
        "KEEP: jornal, portal de notícias, rádio, TV, grupo de mídia. "
        "DROP: post isolado, blog pessoal sem veículo, agregador genérico."
    ),
    # politico e partido = mesmo nicho
    "politico": (
        "KEEP: deputado, senador, vereador, gabinete, partido/diretório, comissão com contato. "
        "DROP: notícia genérica sem pessoa/órgão, meme, fórum."
    ),
    "partido": (
        "KEEP: deputado, senador, vereador, gabinete, partido/diretório, comissão com contato. "
        "DROP: notícia genérica sem pessoa/órgão, meme, fórum."
    ),
    "generalista": (
        "KEEP: qualquer negócio brasileiro ativo (comércio, serviço, indústria, clínica, "
        "loja, oficina, restaurante, escritório, PME) E também órgão público "
        ".gov.br / .leg.br / .jus.br (prefeitura, câmara, tribunal) que possa cotar. "
        "DROP: vaga de emprego, listicle, wiki, empresa estrangeira."
    ),
}

_SYSTEM = (
    "Você é um classificador de leads B2B para prospecção no Brasil. "
    "Responda SOMENTE um objeto JSON válido, sem markdown, sem texto extra."
)


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.05,
    max_tokens: int | None = None,
) -> str:
    """Chamada serial ao LiteLLM. Levanta se falhar."""
    settings = get_settings()
    max_tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens
    url = f"{settings.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    async with llm_semaphore():
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"resposta LLM inválida: {data!r}") from exc


def _niche_rules(niche: str) -> str:
    n = (niche or "").strip().lower()
    return _NICHE_RULES.get(n, "KEEP só se for empresa/org real do nicho. DROP lixo/artigo/wiki.")


def _build_score_prompt(
    *,
    name: str,
    website: str,
    snippet: str,
    niche: str,
    city: str,
    email: str = "",
    stage: str = "",
) -> str:
    return (
        "Classifique se este lead serve para prospecção B2B no nicho indicado.\n"
        f"nicho={niche or '?'}\n"
        f"cidade_alvo={city or '?'}\n"
        f"nome={ (name or '')[:140] }\n"
        f"site={ (website or '')[:160] }\n"
        f"email={ (email or '')[:80] }\n"
        f"stage_atual={stage or '?'}\n"
        f"snippet={ (snippet or '')[:220] }\n"
        f"regras_nicho: {_niche_rules(niche)}\n"
        "DROP sempre: wikipedia, dicio, significados, blog educativo, download, "
        "agência bancária (BB/Itaú/Caixa) se nicho≠banco. "
        "gov.br: DROP nos nichos (exceto generalista e político).\n"
        "Resposta JSON exata com chaves:\n"
        '{"score":0-100,"keep":true|false,"reason":"max 12 palavras",'
        '"clean_name":"nome limpo da empresa ou vazio",'
        '"confidence":"high|medium|low"}'
    )


async def score_company_candidate(
    *,
    name: str,
    website: str = "",
    snippet: str = "",
    niche: str = "",
    city: str = "",
    email: str = "",
    stage: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """
    Score 0–100 se parece lead B2B real do nicho.
    Retorna score, keep, reason, clean_name, confidence, raw.
    force=True ignora HUNT_USE_LLM (para script de revisão).
    Em falha de LLM → keep=True (não bloqueia caçada).
    """
    settings = get_settings()
    if not force and not settings.hunt_use_llm:
        return {
            "score": 50,
            "keep": True,
            "reason": "llm_disabled",
            "clean_name": name or "",
            "confidence": "low",
        }

    prompt = _build_score_prompt(
        name=name,
        website=website,
        snippet=snippet,
        niche=niche,
        city=city,
        email=email,
        stage=stage,
    )
    try:
        raw = await chat_completion(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max(100, settings.llm_max_tokens),
        )
        data = _parse_json(raw)
        score = int(data.get("score", 50))
        score = max(0, min(100, score))
        keep = data.get("keep")
        if keep is None:
            keep = score >= 55
        else:
            keep = bool(keep)
        reason = str(data.get("reason") or "")[:120]
        clean_name = str(data.get("clean_name") or name or "")[:140]
        confidence = str(data.get("confidence") or "medium").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        logger.info(
            "llm_score_company",
            name=(name or "")[:60],
            score=score,
            keep=keep,
            reason=reason,
            confidence=confidence,
        )
        return {
            "score": score,
            "keep": keep,
            "reason": reason,
            "clean_name": clean_name,
            "confidence": confidence,
            "raw": raw[:400],
        }
    except Exception as exc:
        logger.warning("llm_score_failed", error=str(exc), name=(name or "")[:60])
        return {
            "score": 50,
            "keep": True,
            "reason": f"llm_error:{type(exc).__name__}",
            "clean_name": name or "",
            "confidence": "low",
            "error": str(exc)[:200],
        }


async def score_email_belongs_to_business(
    *,
    email: str,
    name: str = "",
    website: str = "",
    city: str = "",
    segment: str = "",
    snippet: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Qwen local: este e-mail genérico (.com/.gmail/…) é contato da empresa BR?

    KEEP se for plausível (PME usa Gmail; empresa BR usa .com/.net/.org).
    DROP se for outra pessoa, outro país, diretório ou sem vínculo.
    Sem LLM / erro → keep=True (não bloqueia a caçada).
    """
    settings = get_settings()
    if not force and not settings.hunt_use_llm:
        return {
            "score": 50,
            "keep": True,
            "reason": "llm_disabled",
            "confidence": "low",
        }

    prompt = (
        "Decida se este E-MAIL é um contato comercial plausível desta empresa brasileira.\n"
        f"empresa={ (name or '')[:140] }\n"
        f"cidade={ (city or '')[:60] }\n"
        f"site={ (website or '')[:160] }\n"
        f"email={ (email or '')[:80] }\n"
        f"nicho={segment or 'generalista'}\n"
        f"snippet={ (snippet or '')[:180] }\n"
        "KEEP: e-mail da empresa (contato@, comercial@) OU gmail/hotmail/outlook "
        "de dono/equipe se o nome bate ou é PME brasileira comum; "
        ".com/.net/.org de empresa BR é normal.\n"
        "DROP: outra pessoa/país sem relação; diretório/vagas/jornal; "
        "e-mail inventado no SERP. "
        "No nicho generalista KEEP e-mail .gov.br/.leg.br/.jus.br (órgão pode cotar).\n"
        "Resposta JSON exata:\n"
        '{"score":0-100,"keep":true|false,"reason":"max 12 palavras",'
        '"confidence":"high|medium|low"}'
    )
    try:
        raw = await chat_completion(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max(80, min(120, settings.llm_max_tokens)),
        )
        data = _parse_json(raw)
        score = max(0, min(100, int(data.get("score", 50))))
        keep = data.get("keep")
        if keep is None:
            keep = score >= 50
        else:
            keep = bool(keep)
        reason = str(data.get("reason") or "")[:120]
        confidence = str(data.get("confidence") or "medium").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        logger.info(
            "llm_score_email",
            email=(email or "")[:60],
            name=(name or "")[:40],
            score=score,
            keep=keep,
            reason=reason,
        )
        return {
            "score": score,
            "keep": keep,
            "reason": reason,
            "confidence": confidence,
            "raw": raw[:300],
        }
    except Exception as exc:
        logger.warning("llm_score_email_failed", error=str(exc), email=(email or "")[:60])
        return {
            "score": 50,
            "keep": True,
            "reason": f"llm_error:{type(exc).__name__}",
            "confidence": "low",
            "error": str(exc)[:200],
        }


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    # remove ```json ... ```
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return {}
