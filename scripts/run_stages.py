#!/usr/bin/env python3
"""Roda campanha em etapas: discover → enrich → crm → [dispatch opcional].

Com REVIEW_QUEUE_ENABLED (default), discover só enfileira. Suba
`python scripts/reviewer.py` para o Qwen gravar ou descartar; o enrich
drena o que já foi aprovado.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService


async def main() -> None:
    p = argparse.ArgumentParser(description="Campanha segmentada por etapas")
    p.add_argument("--name", default="Campanha stages")
    p.add_argument("--niche", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--city", default="Brasil", help="Cidade (default: Brasil = todo o país)")
    p.add_argument("--state", default="", help="UF (vazio = nacional)")
    p.add_argument("-n", "--max", type=int, default=5)
    p.add_argument(
        "--stages",
        default="discover,enrich,crm",
        help="Etapas: discover,enrich,crm,dispatch (csv)",
    )
    p.add_argument("--dispatch", action="store_true", help="Inclui disparo real de e-mail")
    p.add_argument("--dry-run-dispatch", action="store_true")
    p.add_argument("--campaign-id", default="", help="Continua campanha existente")
    args = p.parse_args()

    setup_logging()
    get_settings.cache_clear()
    reset_engine()
    await init_db()
    factory = async_session_factory()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if args.dispatch and "dispatch" not in stages:
        stages.append("dispatch")

    async with factory() as session:
        camp_svc = CampaignService(session)
        stage_svc = StageService(session)

        if args.campaign_id:
            cid = args.campaign_id
            print(f"Campanha existente: {cid}")
        else:
            camp = await camp_svc.create_campaign(
                name=args.name,
                niche=args.niche,
                query=args.query,
                city=args.city,
                state=args.state,
                max_results=args.max,
                config={"skip_email": "dispatch" not in stages or args.dry_run_dispatch},
                run_async=False,
            )
            cid = camp.id
            print(f"Criada campanha {cid} niche={camp.niche}")

        for st in stages:
            print(f"\n=== STAGE {st} ===", flush=True)
            if st == "dispatch":
                campaign = await stage_svc.get_campaign(cid)
                if not campaign:
                    raise SystemExit("campanha não encontrada")
                # se skip_email no config e não dry_run, força dispatch
                cfg = dict(campaign.config or {})
                cfg["skip_email"] = False
                campaign.config = cfg
                result = await stage_svc.stage_dispatch(
                    campaign,
                    dry_run=args.dry_run_dispatch,
                )
            else:
                result = await stage_svc.run_stage(cid, st)
            print(result, flush=True)

            # se enrich pediu mais discover, repete discover+enrich até meta ou teto
            if st == "enrich":
                max_rounds = 5
                rounds = 0
                while result.get("need_more_discover") and rounds < max_rounds:
                    rounds += 1
                    print(
                        f"… enrich incompleto "
                        f"(good={result.get('total_good')}/{result.get('target')}), "
                        f"discover+enrich round {rounds}/{max_rounds}",
                        flush=True,
                    )
                    print(await stage_svc.run_stage(cid, "discover"), flush=True)
                    result = await stage_svc.run_stage(cid, "enrich")
                    print(result, flush=True)

        status = await stage_svc.stage_status(cid)
        print("\n=== STATUS FINAL ===")
        print(status)


if __name__ == "__main__":
    asyncio.run(main())
