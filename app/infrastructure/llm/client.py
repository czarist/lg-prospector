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
_DROP_FOREIGN = (
    "DROP sempre empresa/veículo/marca estrangeira "
    "(Fox News, CNN, BBC, NYT, Microsoft, Oracle, TV dos EUA) — só lead brasileiro."
)
_NICHE_RULES: dict[str, str] = {
    "advogado": (
        "KEEP: escritório de advocacia brasileiro, sociedade de advogados, firmas jurídicas. "
        "DROP: notícias jurídicas genéricas, OAB institucional genérica, fóruns, "
        "artigos 'o que é direito', diretórios sem firma. "
        + _DROP_FOREIGN
    ),
    "agencia_marketing": (
        "KEEP: agência de marketing digital, publicidade, performance, social media, "
        "comunicação/publicidade no Brasil. "
        "DROP: agência BANCÁRIA, correios, lotérica, prefeitura, banco, "
        "lista de agências de banco. "
        + _DROP_FOREIGN
    ),
    "empresa_ti": (
        "KEEP: software house, fábrica de software, consultoria TI, SaaS, "
        "desenvolvimento de sistemas, empresa de tecnologia brasileira. "
        "DROP: wikipedia, dicionário, blog 'o que é software', download Microsoft/ASUS, "
        "marketplace genérico, curso online genérico. "
        + _DROP_FOREIGN
    ),
    "prestador_servico": (
        "KEEP: contabilidade, consultoria empresarial, serviços B2B locais no Brasil. "
        "DROP: artigos, órgãos públicos genéricos, marketplaces. "
        + _DROP_FOREIGN
    ),
    "grupo_midiatico": (
        "KEEP: jornal, portal, rádio, TV ou grupo de mídia BRASILEIRO. "
        "DROP: post isolado, blog pessoal, agregador. "
        + _DROP_FOREIGN
    ),
    # politico e partido = mesmo nicho
    "politico": (
        "KEEP: deputado, senador, vereador, gabinete, partido/diretório, comissão com contato. "
        "DROP: notícia genérica sem pessoa/órgão, meme, fórum, veículo estrangeiro. "
        + _DROP_FOREIGN
    ),
    "partido": (
        "KEEP: deputado, senador, vereador, gabinete, partido/diretório, comissão com contato. "
        "DROP: notícia genérica sem pessoa/órgão, meme, fórum, veículo estrangeiro. "
        + _DROP_FOREIGN
    ),
    "generalista": (
        "KEEP: qualquer negócio brasileiro ativo (comércio, serviço, indústria, clínica, "
        "loja, oficina, restaurante, escritório, PME) E também órgão público "
        ".gov.br / .leg.br / .jus.br (prefeitura, câmara, tribunal) que possa cotar. "
        "DROP: vaga de emprego, listicle, wiki. "
        + _DROP_FOREIGN
    ),
}

_SYSTEM = (
    "Você é um classificador de leads B2B para prospecção no Brasil. "
    "Responda SOMENTE um objeto JSON válido, sem markdown, sem texto extra."
)


_LOCAL_MODEL_MARKERS = ("local-main", "qwen", "gguf")


def is_local_llm_model(model: str) -> bool:
    m = (model or "").strip().lower()
    return any(x in m for x in _LOCAL_MODEL_MARKERS)


def _llm_chain(
    primary: str,
    fallbacks: list[str] | None,
    *,
    forbid_local: bool,
) -> list[str]:
    chain: list[str] = []
    for m in [primary, *(fallbacks or [])]:
        name = (m or "").strip()
        if not name or name in chain:
            continue
        if forbid_local and is_local_llm_model(name):
            continue
        chain.append(name)
    return chain


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.05,
    max_tokens: int | None = None,
    model: str | None = None,
    fallbacks: list[str] | None = None,
    forbid_local: bool = False,
    timeout_seconds: float | None = None,
) -> str:
    """Chamada serial ao LiteLLM. Levanta se falhar.

    `model` / `fallbacks` escolhem o id LiteLLM (groq-fast, gemini-free…).
    `forbid_local=True` recusa Qwen/local-main e desliga o fallback do proxy.
    """
    text, _used = await chat_completion_used(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        fallbacks=fallbacks,
        forbid_local=forbid_local,
        timeout_seconds=timeout_seconds,
    )
    return text


async def chat_completion_used(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.05,
    max_tokens: int | None = None,
    model: str | None = None,
    fallbacks: list[str] | None = None,
    forbid_local: bool = False,
    timeout_seconds: float | None = None,
) -> tuple[str, str]:
    """Como chat_completion, mas devolve (texto, modelo_que_respondeu)."""
    settings = get_settings()
    max_tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(settings.llm_timeout_seconds)
    )
    primary = (model or settings.model or "").strip()
    chain = _llm_chain(primary, fallbacks, forbid_local=forbid_local)
    if not chain:
        raise RuntimeError("nenhum modelo LiteLLM elegível (local bloqueado?)")

    url = f"{settings.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    last_err: Exception | None = None
    async with llm_semaphore():
        async with httpx.AsyncClient(timeout=timeout) as client:
            for name in chain:
                payload = {
                    "model": name,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                # o proxy LiteLLM tem fallback p/ local-main; no auditor isso não pode
                if forbid_local:
                    payload["disable_fallbacks"] = True
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    replied = str(data.get("model") or name)
                    if forbid_local and is_local_llm_model(replied):
                        raise RuntimeError(f"proxy devolveu modelo local={replied}")
                    content = (data["choices"][0]["message"].get("content") or "").strip()
                    if not content:
                        raise RuntimeError(f"resposta vazia model={name}")
                    if name != chain[0]:
                        logger.info("llm_fallback_used", wanted=chain[0], used=name)
                    return content, name
                except Exception as exc:
                    last_err = exc
                    logger.warning("llm_model_failed", model=name, error=str(exc)[:180])
                    continue
    raise RuntimeError(f"todos os modelos falharam: {last_err}") from last_err


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
        "agência bancária (BB/Itaú/Caixa) se nicho≠banco, "
        "empresa/veículo estrangeiro (Fox News, CNN.com, BBC, NYT). "
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


_EMPTY_SALVAGE = {
    "keep": True,
    "email_action": "keep",
    "suggested_email": "",
    "clean_name": "",
    "website": "",
    "city": "",
    "state": "",
    "phone": "",
    "contact_name": "",
    "role": "",
    "linkedin": "",
    "analysis": "",
    "gaps_filled": [],
    "reason": "",
    "confidence": "low",
    "valid_company": True,
}


async def salvage_saved_contact(
    *,
    name: str,
    website: str = "",
    email: str = "",
    city: str = "",
    state: str = "",
    niche: str = "",
    bounce: dict[str, Any] | None = None,
    candidates: list[str] | None = None,
    search_notes: str = "",
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    """Modelos REMOTOS (LiteLLM). Nunca Qwen local.

    Objetivo: recuperar o contato e tapar lacunas com evidência. JSON only.
    """
    settings = get_settings()
    bounce = bounce or {}
    cands = [c for c in (candidates or []) if c][:10]
    gap_list = [g for g in (gaps or []) if g][:12]
    bounce_txt = ""
    if bounce:
        bounce_txt = (
            f"bounce_class={bounce.get('classification') or ''} "
            f"diag={(str(bounce.get('diagnostic') or bounce.get('error') or ''))[:180]}"
        )
    prompt = (
        "Você é analista de leads B2B no Brasil. O contato JÁ está cadastrado.\n"
        "NÃO recomende apagar. Faça uma leitura do dossiê (site + busca + bounce).\n"
        "1) analysis: 3 a 6 frases — o que a empresa faz, se é brasileira, "
        "qualidade do contato, o que estava vazio e o que o dossiê confirma.\n"
        "2) Enriqueça SÓ campos listados em lacunas, e SÓ com dado que apareça "
        "no dossiê (título, snippet, sample do site, telefone extraído). "
        "Não invente CNPJ, telefone, cidade, nome de pessoa, LinkedIn ou site.\n"
        "Campo sem evidência = string vazia. gaps_filled = chaves que você "
        "preencheu de fato.\n"
        "E-mail: email_action=replace se (a) bounce 'caixa não existe' OU "
        "(b) o e-mail atual é inválido / não pertence à empresa E há "
        "candidatos_email. suggested_email = melhor da lista, nunca invente. "
        "Se o e-mail atual serve, email_action=keep.\n"
        f"empresa={(name or '')[:160]}\n"
        f"cidade={(city or '')[:80]}\n"
        f"uf={(state or '')[:8]}\n"
        f"site={(website or '')[:180]}\n"
        f"email_atual={(email or '')[:80]}\n"
        f"nicho={(niche or '')[:40]}\n"
        f"lacunas={', '.join(gap_list) or 'nenhuma'}\n"
        f"{bounce_txt}\n"
        f"candidatos_email={', '.join(cands) or 'nenhum'}\n"
        f"dossie={(search_notes or '')[:3200]}\n"
        "Resposta SOMENTE JSON:\n"
        '{"keep":true,"email_action":"keep|replace","suggested_email":"",'
        '"clean_name":"","website":"","city":"","state":"",'
        '"phone":"","contact_name":"","role":"","linkedin":"",'
        '"analysis":"3 a 6 frases","gaps_filled":["phone"],'
        '"reason":"max 28 palavras","confidence":"high|medium|low",'
        '"valid_company":true}'
    )
    fallbacks = [
        x.strip()
        for x in (settings.auditor_fallback_models or "").split(",")
        if x.strip()
    ]
    try:
        raw, used = await chat_completion_used(
            [
                {
                    "role": "system",
                    "content": (
                        "Você recupera contatos comerciais no Brasil. "
                        "Nunca diga para excluir o lead. "
                        "Responda SOMENTE JSON válido."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.15,
            max_tokens=max(400, int(settings.auditor_max_tokens)),
            model=settings.auditor_model,
            fallbacks=fallbacks,
            forbid_local=True,
            timeout_seconds=float(settings.auditor_timeout_seconds),
        )
        data = _parse_json(raw)
        action = str(data.get("email_action") or "keep").strip().lower()
        if action not in {"keep", "replace"}:
            action = "keep"
        suggested = str(data.get("suggested_email") or "").strip().lower()
        conf = str(data.get("confidence") or "medium").lower()
        if conf not in {"high", "medium", "low"}:
            conf = "medium"
        keep = data.get("keep")
        keep = True if keep is None else bool(keep)
        valid = data.get("valid_company")
        valid = True if valid is None else bool(valid)
        gaps = data.get("gaps_filled")
        if not isinstance(gaps, list):
            gaps = []
        return {
            **_EMPTY_SALVAGE,
            "keep": keep,
            "email_action": action,
            "suggested_email": suggested,
            "clean_name": str(data.get("clean_name") or "").strip()[:191],
            "website": str(data.get("website") or "").strip()[:512],
            "city": str(data.get("city") or "").strip()[:128],
            "state": str(data.get("state") or "").strip()[:8].upper(),
            "phone": str(data.get("phone") or "").strip()[:64],
            "contact_name": str(data.get("contact_name") or "").strip()[:255],
            "role": str(data.get("role") or "").strip()[:128],
            "linkedin": str(data.get("linkedin") or "").strip()[:512],
            "analysis": str(data.get("analysis") or "").strip()[:1800],
            "gaps_filled": [str(g)[:40] for g in gaps[:12]],
            "reason": str(data.get("reason") or "")[:200],
            "confidence": conf,
            "valid_company": valid,
            "model": used,
            "raw": raw[:1200],
        }
    except Exception as exc:
        logger.warning("auditor_llm_failed", error=str(exc), name=(name or "")[:60])
        return {
            **_EMPTY_SALVAGE,
            "reason": f"llm_error:{type(exc).__name__}",
            "error": str(exc)[:200],
        }


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return {}
