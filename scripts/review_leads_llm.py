#!/usr/bin/env python3
"""Revisa leads já gravados com Qwen local (LiteLLM).

Uso:
  python scripts/review_leads_llm.py              # todos os recentes
  python scripts/review_leads_llm.py --niche advogado -n 20
  python scripts/review_leads_llm.py --campaign-id <id>
  python scripts/review_leads_llm.py --apply-discard   # marca discarded se keep=false e score<40
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.paths import logs_dir
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.models import Campaign, CampaignItem, Company, ItemStatus
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.infrastructure.llm.client import score_company_candidate

logger = get_logger(__name__)


async def main() -> None:
    p = argparse.ArgumentParser(description="Revisão LLM de leads existentes")
    p.add_argument("--niche", default="", help="Filtra por nicho da campanha")
    p.add_argument("--campaign-id", default="", help="Filtra por campanha")
    p.add_argument("-n", "--limit", type=int, default=40, help="Máx. itens a revisar")
    p.add_argument(
        "--stages",
        default="crm_synced,enriched,sent,discovered,discarded",
        help="Stages a incluir (csv)",
    )
    p.add_argument(
        "--apply-discard",
        action="store_true",
        help="Se keep=false e score<40, marca item como discarded (não apaga CRM)",
    )
    p.add_argument(
        "--min-discard-score",
        type=int,
        default=40,
        help="Score abaixo disto + keep=false → discard se --apply-discard",
    )
    p.add_argument(
        "--out",
        default=str(logs_dir() / "llm_review.jsonl"),
        help="Arquivo JSONL de saída",
    )
    args = p.parse_args()

    setup_logging()
    get_settings.cache_clear()
    settings = get_settings()
    print(
        f"LLM model={settings.model} base={settings.base_url} "
        f"hunt_use_llm={settings.hunt_use_llm}",
        flush=True,
    )

    reset_engine()
    await init_db()
    factory = async_session_factory()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with factory() as session:
        q = (
            select(CampaignItem)
            .join(Campaign, CampaignItem.campaign_id == Campaign.id)
            .options(
                selectinload(CampaignItem.company),
                selectinload(CampaignItem.campaign),
            )
            .where(CampaignItem.stage.in_(stages))
            .order_by(CampaignItem.created_at.desc())
            .limit(args.limit * 3)  # puxa a mais; filtra niche depois
        )
        if args.campaign_id:
            q = q.where(CampaignItem.campaign_id == args.campaign_id)
        if args.niche:
            q = q.where(Campaign.niche == args.niche)

        items = (await session.execute(q)).scalars().all()
        # dedupe por company_id / email
        seen: set[str] = set()
        selected: list[CampaignItem] = []
        for it in items:
            co = it.company
            key = (co.email or co.website or co.name or it.id) if co else it.id
            key = str(key).lower()
            if key in seen:
                continue
            seen.add(key)
            selected.append(it)
            if len(selected) >= args.limit:
                break

        print(f"Revisando {len(selected)} itens…", flush=True)

        by_verdict: Counter[str] = Counter()
        by_niche: dict[str, Counter] = defaultdict(Counter)
        results: list[dict] = []
        applied = 0

        for i, it in enumerate(selected, 1):
            camp = it.campaign
            co = it.company
            niche = (camp.niche if camp else "") or (co.segment if co else "") or ""
            name = (co.name if co else "") or ""
            website = (co.website if co else "") or ""
            email = (co.email if co else "") or ""
            city = (co.city if co else "") or (camp.city if camp else "") or ""
            snippet = ""
            raw = it.raw_data or {}
            if isinstance(raw, dict):
                snippet = str(raw.get("snippet") or (raw.get("extra") or {}).get("snippet") or "")
                if not snippet:
                    snippet = str(raw.get("qualification_notes") or it.qualification_notes or "")

            score = await score_company_candidate(
                name=name,
                website=website,
                snippet=snippet,
                niche=niche,
                city=city,
                email=email,
                stage=it.stage or "",
                force=True,  # revisão sempre chama o modelo
            )

            verdict = "keep" if score.get("keep") else "drop"
            by_verdict[verdict] += 1
            by_niche[niche][verdict] += 1

            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "item_id": it.id,
                "campaign_id": it.campaign_id,
                "niche": niche,
                "stage": it.stage,
                "name": name,
                "website": website,
                "email": email,
                "city": city,
                "llm": {
                    "score": score.get("score"),
                    "keep": score.get("keep"),
                    "reason": score.get("reason"),
                    "clean_name": score.get("clean_name"),
                    "confidence": score.get("confidence"),
                    "error": score.get("error"),
                },
            }
            results.append(row)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            flag = "✓" if score.get("keep") else "✗"
            print(
                f"[{i}/{len(selected)}] {flag} score={score.get('score'):>3} "
                f"conf={score.get('confidence')} niche={niche:18} "
                f"{name[:45]!r} | {score.get('reason')}",
                flush=True,
            )

            if (
                args.apply_discard
                and not score.get("keep")
                and int(score.get("score") or 0) < args.min_discard_score
                and it.stage
                not in {
                    ItemStageStatus.DISCARDED.value,
                    ItemStageStatus.SENT.value,
                }
            ):
                it.stage = ItemStageStatus.DISCARDED.value
                it.status = ItemStatus.SKIPPED.value
                notes = (it.qualification_notes or "") + (
                    f" | llm_review drop score={score.get('score')} "
                    f"reason={score.get('reason')}"
                )
                it.qualification_notes = notes[:500]
                raw_data = dict(it.raw_data or {})
                raw_data["llm_review"] = score
                it.raw_data = raw_data
                applied += 1

            # respiro pro 7B
            await asyncio.sleep(0.15)

        if args.apply_discard and applied:
            await session.commit()
            print(f"Aplicados {applied} discards locais.", flush=True)
        else:
            await session.rollback()

    # parecer
    print("\n=== PARECER REVISÃO LLM ===")
    total = len(results)
    keep_n = by_verdict.get("keep", 0)
    drop_n = by_verdict.get("drop", 0)
    print(f"Total revisados: {total}")
    print(f"Keep: {keep_n} ({100*keep_n/max(total,1):.0f}%)")
    print(f"Drop: {drop_n} ({100*drop_n/max(total,1):.0f}%)")
    print("\nPor nicho:")
    for niche, c in sorted(by_niche.items()):
        t = sum(c.values())
        print(f"  {niche or '?':20} keep={c['keep']:3} drop={c['drop']:3} (n={t})")

    # amostras drop
    drops = [r for r in results if not r["llm"].get("keep")]
    keeps = [r for r in results if r["llm"].get("keep")]
    print("\nExemplos DROP (até 8):")
    for r in drops[:8]:
        print(f"  - [{r['niche']}] {r['name'][:50]!r} score={r['llm']['score']} — {r['llm']['reason']}")
    print("\nExemplos KEEP (até 8):")
    for r in keeps[:8]:
        print(f"  - [{r['niche']}] {r['name'][:50]!r} score={r['llm']['score']} — {r['llm']['reason']}")

    # scores médios
    scores = [int(r["llm"]["score"] or 0) for r in results if r["llm"].get("score") is not None]
    if scores:
        print(f"\nScore médio: {sum(scores)/len(scores):.1f}  min={min(scores)} max={max(scores)}")
    errors = [r for r in results if r["llm"].get("error") or str(r["llm"].get("reason","")).startswith("llm_error")]
    print(f"Erros LLM: {len(errors)}")
    print(f"Log: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
