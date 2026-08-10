#!/usr/bin/env python3
"""Loop contínuo de prospecção: 5 leads × nicho × cidade (escada comercial).

Ordem de cidades (escadinha):
  1. Capitais (Porto Alegre / RS primeiro se --focus-rs)
  2. Polos gaúchos (Caxias, Canoas, Pelotas, …)
  3. Outras cidades de alto pop/PIB
  4. Tier 3 só com --max-tier 3

Não gasta rodada em município pequeno (min pop default 90k, exceto capitais).

Estado persistido em logs/hunt_loop_state.json (retoma após restart).

Exemplos:
  # foco RS + capitais, 5 por nicho, sem fim
  python scripts/hunt_loop.py --focus-rs --max-tier 2 -n 5

  # só RS (sem SP/RJ…), uma volta e para
  python scripts/hunt_loop.py --only-rs --once

  # dry-run: só imprime a fila
  python scripts/hunt_loop.py --plan-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.domain.cities import (
    DEFAULT_NICHES,
    CityTarget,
    build_city_queue,
    pick_query,
)
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService

logger = get_logger(__name__)

STATE_PATH = ROOT / "logs" / "hunt_loop_state.json"
LOG_DIR = ROOT / "logs" / "hunt"
DEFAULT_STAGES = ["discover", "enrich", "crm", "dispatch"]

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[hunt_loop] sinal {signum} — finalizando após job atual…", flush=True)


class HuntFileLogger:
    """Dois arquivos por dia: results + errors (JSONL) e espelho texto legível."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = run_id
        self.results_jsonl = self.log_dir / f"results_{day}.jsonl"
        self.errors_jsonl = self.log_dir / f"errors_{day}.jsonl"
        self.results_txt = self.log_dir / f"results_{day}.log"
        self.errors_txt = self.log_dir / f"errors_{day}.log"
        self.run_txt = self.log_dir / f"run_{run_id}.log"
        # cabeçalho da corrida
        header = (
            f"\n{'='*72}\n"
            f"HUNT RUN {run_id} started {datetime.now(timezone.utc).isoformat()}\n"
            f"{'='*72}\n"
        )
        self.run_txt.write_text(header, encoding="utf-8")
        with self.results_txt.open("a", encoding="utf-8") as f:
            f.write(header)
        with self.errors_txt.open("a", encoding="utf-8") as f:
            f.write(header)

    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        row = {**row, "run_id": self.run_id, "ts": datetime.now(timezone.utc).isoformat()}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _append_txt(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    def log_console(self, msg: str) -> None:
        """Espelha linha no run_*.log (além do print do caller)."""
        self._append_txt(self.run_txt, msg)

    def result(self, row: dict[str, Any]) -> None:
        self._append_jsonl(self.results_jsonl, row)
        line = (
            f"[{row.get('ts') or datetime.now().isoformat()}] "
            f"OK niche={row.get('niche')} city={row.get('city')}/{row.get('state')} "
            f"good={row.get('good')}/{row.get('target')} "
            f"emails={row.get('emails_sent', 0)} cooldown={row.get('emails_cooldown', 0)} "
            f"full={row.get('full')} campaign={str(row.get('campaign_id') or '')[:12]} "
            f"elapsed={row.get('elapsed_s')}s"
        )
        self._append_txt(self.results_txt, line)
        self._append_txt(self.run_txt, line)

    def error(self, row: dict[str, Any]) -> None:
        self._append_jsonl(self.errors_jsonl, row)
        line = (
            f"[{datetime.now().isoformat()}] "
            f"ERR niche={row.get('niche')} city={row.get('city')}/{row.get('state')} "
            f"error={row.get('error') or row.get('message') or 'unknown'}"
        )
        self._append_txt(self.errors_txt, line)
        self._append_txt(self.run_txt, line)

    def stage(self, *, niche: str, city: str, state: str, stage: str, result: dict) -> None:
        """Log intermediário de cada etapa (também no results jsonl como event)."""
        row = {
            "event": "stage",
            "niche": niche,
            "city": city,
            "state": state,
            "stage": stage,
            "result": result,
        }
        self._append_jsonl(self.results_jsonl, row)
        line = f"  stage={stage} → {json.dumps(result, ensure_ascii=False, default=str)[:400]}"
        self._append_txt(self.results_txt, line)
        self._append_txt(self.run_txt, line)

    def summary(self, text: str) -> None:
        self._append_txt(self.results_txt, text)
        self._append_txt(self.run_txt, text)
        print(text, flush=True)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "completed": {},  # key niche|city|UF → meta
            "failed": {},
            "cursor": 0,
            "cycle": 0,
            "updated_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "version": 1,
            "completed": {},
            "failed": {},
            "cursor": 0,
            "cycle": 0,
            "updated_at": None,
        }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _job_key(niche: str, city: CityTarget) -> str:
    return f"{niche}|{city.city}|{city.state}".lower()


def _build_jobs(
    cities: list[CityTarget],
    niches: list[str],
    *,
    city_first: bool = True,
) -> list[tuple[str, CityTarget]]:
    """city_first=True: esgota todos nichos de uma cidade antes da próxima
    (melhor cache local / menos troca de contexto SERP)."""
    jobs: list[tuple[str, CityTarget]] = []
    if city_first:
        for city in cities:
            for niche in niches:
                jobs.append((niche, city))
    else:
        for niche in niches:
            for city in cities:
                jobs.append((niche, city))
    return jobs


async def _run_one(
    *,
    niche: str,
    city: CityTarget,
    max_results: int,
    stages: list[str],
    query_round: int,
    dry_run_dispatch: bool = False,
    cooldown_days: int = 4,
    file_log: HuntFileLogger | None = None,
) -> dict[str, Any]:
    get_settings.cache_clear()
    reset_engine()
    await init_db()
    factory = async_session_factory()

    query = pick_query(niche, city.city, query_round)
    name = f"Hunt {niche} {city.label} {datetime.now().strftime('%m%d-%H%M')}"
    want_dispatch = "dispatch" in stages

    def _echo(msg: str) -> None:
        print(msg, flush=True)
        if file_log:
            file_log.log_console(msg)

    async with factory() as session:
        camp_svc = CampaignService(session)
        stage_svc = StageService(session)

        camp = await camp_svc.create_campaign(
            name=name,
            niche=niche,
            query=query,
            city=city.city,
            state=city.state,
            max_results=max_results,
            config={
                # liberar dispatch se estiver na lista de stages
                "skip_email": not want_dispatch,
                "hunt_loop": True,
                "city_tier": city.tier,
                "city_pop_k": city.population_k,
            },
            run_async=False,
        )
        cid = camp.id
        _echo(f"  campanha {cid[:12]}… query={query!r}")

        stage_results: dict[str, Any] = {}
        for st in stages:
            if _stop:
                break
            _echo(f"  === {st} ===")
            try:
                if st == "dispatch":
                    campaign = await stage_svc.get_campaign(cid)
                    if not campaign:
                        raise RuntimeError(f"campanha {cid} não encontrada")
                    cfg = dict(campaign.config or {})
                    cfg["skip_email"] = False
                    campaign.config = cfg
                    result = await stage_svc.stage_dispatch(
                        campaign,
                        dry_run=dry_run_dispatch,
                        cooldown_days=cooldown_days,
                    )
                else:
                    result = await stage_svc.run_stage(cid, st)
            except Exception as stage_exc:
                err = {
                    "event": "stage_error",
                    "niche": niche,
                    "city": city.city,
                    "state": city.state,
                    "campaign_id": cid,
                    "stage": st,
                    "error": str(stage_exc),
                    "error_type": type(stage_exc).__name__,
                }
                if file_log:
                    file_log.error(err)
                _echo(f"  ✗ stage {st} falhou: {stage_exc}")
                raise

            stage_results[st] = result
            _echo(f"  {result}")
            if file_log:
                file_log.stage(
                    niche=niche,
                    city=city.city,
                    state=city.state,
                    stage=st,
                    result=result if isinstance(result, dict) else {"raw": str(result)},
                )

            if st == "enrich" and result.get("need_more_discover"):
                for r in range(3):
                    if _stop:
                        break
                    if not result.get("need_more_discover"):
                        break
                    _echo(f"  … reforço discover/enrich {r+1}/3")
                    try:
                        await stage_svc.run_stage(cid, "discover")
                        result = await stage_svc.run_stage(cid, "enrich")
                    except Exception as reforco_exc:
                        # não deixa um reforço falho derrubar o job inteiro:
                        # segue com o que já foi enriquecido até aqui (crm/dispatch).
                        # rollback é obrigatório: uma exceção a meio de run_stage()
                        # deixa a sessão numa transação abortada.
                        await session.rollback()
                        err = {
                            "event": "stage_error",
                            "niche": niche,
                            "city": city.city,
                            "state": city.state,
                            "campaign_id": cid,
                            "stage": "enrich_retry",
                            "error": str(reforco_exc),
                            "error_type": type(reforco_exc).__name__,
                        }
                        if file_log:
                            file_log.error(err)
                        _echo(f"  ✗ reforço discover/enrich falhou (seguindo com o que há): {reforco_exc}")
                        break
                    stage_results["enrich"] = result
                    _echo(f"  {result}")
                    if file_log:
                        file_log.stage(
                            niche=niche,
                            city=city.city,
                            state=city.state,
                            stage="enrich_retry",
                            result=result if isinstance(result, dict) else {"raw": str(result)},
                        )

        status = await stage_svc.stage_status(cid)
        await session.commit()

    by = status.get("items_by_stage") or {}
    good = int(
        by.get(ItemStageStatus.CRM_SYNCED.value, 0)
        + by.get(ItemStageStatus.ENRICHED.value, 0)
        + by.get(ItemStageStatus.SENT.value, 0)
    )
    dispatch_info = stage_results.get("dispatch") or {}
    return {
        "event": "job_done",
        "campaign_id": cid,
        "niche": niche,
        "city": city.city,
        "state": city.state,
        "query": query,
        "good": good,
        "target": max_results,
        "items_by_stage": by,
        "stages": {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "raw"}
            if isinstance(v, dict)
            else v
            for k, v in stage_results.items()
        },
        "emails_sent": int(dispatch_info.get("sent") or 0),
        "emails_failed": int(dispatch_info.get("failed") or 0),
        "emails_cooldown": int(dispatch_info.get("cooldown_skipped") or 0),
        "template": dispatch_info.get("template"),
        "ok": good >= max(1, max_results // 2),
        "full": good >= max_results,
    }


async def main() -> None:
    global _stop

    p = argparse.ArgumentParser(description="Loop de caçada multi-nicho / multi-cidade")
    p.add_argument("-n", "--max", type=int, default=5, help="Leads por nicho×cidade")
    p.add_argument(
        "--niches",
        default=",".join(DEFAULT_NICHES),
        help="Nichos csv (default: todos)",
    )
    p.add_argument("--focus-rs", action="store_true", default=True, help="Prioriza RS (default on)")
    p.add_argument("--no-focus-rs", action="store_true", help="Desliga prioridade RS")
    p.add_argument("--only-rs", action="store_true", help="Só cidades do RS")
    p.add_argument("--only-capitals", action="store_true", help="Só capitais + DF")
    p.add_argument("--max-tier", type=int, default=2, choices=[1, 2, 3])
    p.add_argument(
        "--min-pop-k",
        type=int,
        default=90,
        help="População mínima em mil (polos); capitais sempre entram",
    )
    p.add_argument("--city-limit", type=int, default=0, help="Máx cidades na fila (0=todas)")
    p.add_argument(
        "--states",
        default="",
        help="Filtra UFs csv (ex: RS,SC,PR). Vazio = todas da escada",
    )
    p.add_argument(
        "--stages",
        default="discover,enrich,crm,dispatch",
        help="Etapas csv (default inclui dispatch de e-mail por nicho)",
    )
    p.add_argument(
        "--dry-run-dispatch",
        action="store_true",
        help="Monta e-mails mas não envia via SMTP",
    )
    p.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Não envia e-mail (só discover→enrich→crm)",
    )
    p.add_argument(
        "--cooldown-days",
        type=int,
        default=4,
        help="Não reenviar pro mesmo contato em menos de N dias (default 4)",
    )
    p.add_argument("--pause", type=float, default=8.0, help="Pausa entre jobs (s)")
    p.add_argument("--cycle-pause", type=float, default=60.0, help="Pausa entre voltas (s)")
    p.add_argument("--once", action="store_true", help="Uma volta na fila e encerra")
    p.add_argument("--max-jobs", type=int, default=0, help="Para após N jobs (0=∞)")
    p.add_argument(
        "--skip-completed",
        action="store_true",
        default=True,
        help="Pula niche×cidade já completo (default)",
    )
    p.add_argument(
        "--redo",
        action="store_true",
        help="Refaz mesmo se já completo no state",
    )
    p.add_argument(
        "--require-full",
        action="store_true",
        help="Só marca complete se atingiu N leads; senão re-tenta depois",
    )
    p.add_argument("--plan-only", action="store_true", help="Só lista a fila e sai")
    p.add_argument("--state-file", default=str(STATE_PATH))
    p.add_argument(
        "--log-dir",
        default=str(LOG_DIR),
        help="Pasta dos logs results/errors (default logs/hunt)",
    )
    p.add_argument(
        "--niche-first",
        action="store_true",
        help="Alterna cidade dentro do nicho (default: cidade primeiro)",
    )
    args = p.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    setup_logging()
    get_settings.cache_clear()
    settings = get_settings()

    focus_rs = args.focus_rs and not args.no_focus_rs
    niches = [n.strip() for n in args.niches.split(",") if n.strip()]
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if args.no_dispatch:
        stages = [s for s in stages if s != "dispatch"]
    elif "dispatch" not in stages and not args.no_dispatch:
        stages.append("dispatch")
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()] or None
    city_limit = args.city_limit or None

    cities = build_city_queue(
        focus_rs=focus_rs,
        max_tier=args.max_tier,
        min_population_k=args.min_pop_k,
        only_rs=args.only_rs,
        only_capitals=args.only_capitals,
        limit=city_limit,
        include_states=states,
    )
    jobs = _build_jobs(cities, niches, city_first=not args.niche_first)

    print("=== HUNT LOOP ===", flush=True)
    print(
        f"backend={settings.search_backend} serper={'yes' if settings.serper_api_key else 'no'} "
        f"llm={settings.hunt_use_llm} model={settings.model}",
        flush=True,
    )
    print(
        f"niches={niches} | cidades={len(cities)} | jobs/volta={len(jobs)} | n={args.max}",
        flush=True,
    )
    print(
        f"stages={stages} | cooldown={args.cooldown_days}d | "
        f"dry_run_dispatch={args.dry_run_dispatch}",
        flush=True,
    )
    print(
        f"focus_rs={focus_rs} only_rs={args.only_rs} max_tier={args.max_tier} "
        f"min_pop_k={args.min_pop_k}",
        flush=True,
    )
    print("Fila (primeiras 25 cidades):", flush=True)
    for i, c in enumerate(cities[:25], 1):
        print(
            f"  {i:2}. [T{c.tier}] {c.label:28} pop~{c.population_k}k  {c.notes}",
            flush=True,
        )
    if len(cities) > 25:
        print(f"  … +{len(cities)-25} cidades", flush=True)

    if args.plan_only:
        print(f"\nTotal jobs planejados por volta: {len(jobs)}")
        for niche, city in jobs[:30]:
            print(f"  - {niche:20} @ {city.label}")
        return

    file_log = HuntFileLogger(Path(args.log_dir) if args.log_dir else LOG_DIR)
    print(f"logs → {file_log.log_dir}", flush=True)
    print(f"  results: {file_log.results_jsonl.name} / {file_log.results_txt.name}", flush=True)
    print(f"  errors:  {file_log.errors_jsonl.name} / {file_log.errors_txt.name}", flush=True)
    print(f"  run:     {file_log.run_txt.name}", flush=True)
    file_log.log_console(
        f"config backend={settings.search_backend} niches={niches} "
        f"cities={len(cities)} jobs={len(jobs)} n={args.max} stages={stages} "
        f"cooldown={args.cooldown_days}d dry_run={args.dry_run_dispatch}"
    )

    state_path = Path(args.state_file)
    state = _load_state(state_path)
    completed: dict = state.setdefault("completed", {})
    failed: dict = state.setdefault("failed", {})

    jobs_done = 0
    jobs_ok = 0
    jobs_err = 0
    emails_total = 0
    cycle = int(state.get("cycle") or 0)

    while not _stop:
        cycle += 1
        state["cycle"] = cycle
        print(f"\n######## CICLO {cycle} ########", flush=True)
        file_log.log_console(f"######## CICLO {cycle} ########")
        cycle_hits = 0

        for idx, (niche, city) in enumerate(jobs):
            if _stop:
                break
            if args.max_jobs and jobs_done >= args.max_jobs:
                print("max-jobs atingido", flush=True)
                _stop = True
                break

            key = _job_key(niche, city)
            if not args.redo and args.skip_completed and key in completed:
                prev = completed[key]
                if args.require_full and not prev.get("full"):
                    pass  # reprocessa incompletos
                else:
                    continue

            header = (
                f"\n--- [{idx+1}/{len(jobs)}] {niche} @ {city.label} "
                f"(T{city.tier} pop~{city.population_k}k) ---"
            )
            print(header, flush=True)
            file_log.log_console(header)
            t0 = time.monotonic()
            try:
                result = await _run_one(
                    niche=niche,
                    city=city,
                    max_results=args.max,
                    stages=stages,
                    query_round=cycle + idx,
                    dry_run_dispatch=args.dry_run_dispatch,
                    cooldown_days=args.cooldown_days,
                    file_log=file_log,
                )
                elapsed = time.monotonic() - t0
                result["elapsed_s"] = round(elapsed, 1)
                result["cycle"] = cycle
                result["job_key"] = key

                # sucesso / parcial → results log
                file_log.result(result)
                emails_total += int(result.get("emails_sent") or 0)

                if result.get("full") or (result.get("ok") and not args.require_full):
                    completed[key] = {
                        "campaign_id": result["campaign_id"],
                        "good": result["good"],
                        "full": result.get("full"),
                        "emails_sent": result.get("emails_sent"),
                        "at": datetime.now(timezone.utc).isoformat(),
                        "cycle": cycle,
                    }
                    failed.pop(key, None)
                    jobs_ok += 1
                    msg = (
                        f"  ✓ good={result['good']}/{args.max} "
                        f"emails={result.get('emails_sent', 0)} "
                        f"cooldown_skip={result.get('emails_cooldown', 0)} "
                        f"full={result.get('full')} em {elapsed:.0f}s"
                    )
                    print(msg, flush=True)
                    file_log.log_console(msg)
                else:
                    # parcial fraco: results + errors (para amanhã revisar)
                    failed[key] = {
                        "good": result["good"],
                        "at": datetime.now(timezone.utc).isoformat(),
                        "items": result.get("items_by_stage"),
                    }
                    file_log.error(
                        {
                            "event": "job_partial",
                            "niche": niche,
                            "city": city.city,
                            "state": city.state,
                            "campaign_id": result.get("campaign_id"),
                            "good": result.get("good"),
                            "target": result.get("target"),
                            "items_by_stage": result.get("items_by_stage"),
                            "message": "meta incompleta",
                        }
                    )
                    msg = (
                        f"  ~ parcial good={result['good']}/{args.max} "
                        f"— re-tentará em outro ciclo"
                    )
                    print(msg, flush=True)
                    file_log.log_console(msg)

                cycle_hits += 1
                jobs_done += 1
            except Exception as exc:
                jobs_err += 1
                jobs_done += 1
                logger.exception("hunt_job_failed", niche=niche, city=city.label)
                failed[key] = {
                    "error": str(exc)[:300],
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                file_log.error(
                    {
                        "event": "job_error",
                        "niche": niche,
                        "city": city.city,
                        "state": city.state,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "cycle": cycle,
                        "job_key": key,
                    }
                )
                print(f"  ✗ erro: {exc}", flush=True)

            state["completed"] = completed
            state["failed"] = failed
            state["cursor"] = idx
            _save_state(state_path, state)

            if not _stop and args.pause > 0:
                await asyncio.sleep(args.pause)

        summary = (
            f"\n[ciclo {cycle}] jobs_nesta_volta≈{cycle_hits} | "
            f"completed={len(completed)} failed_keys={len(failed)} | "
            f"ok={jobs_ok} err={jobs_err} emails_total={emails_total}"
        )
        file_log.summary(summary)
        _save_state(state_path, state)

        if args.once or _stop:
            break

        pending = [
            (n, c)
            for n, c in jobs
            if _job_key(n, c) not in completed
            or (args.require_full and not completed.get(_job_key(n, c), {}).get("full"))
        ]
        if not pending and not args.redo:
            file_log.summary("Fila completa — nada pendente. Encerrando.")
            break

        if args.cycle_pause > 0 and not _stop:
            msg = f"Pausa entre ciclos {args.cycle_pause}s…"
            print(msg, flush=True)
            file_log.log_console(msg)
            await asyncio.sleep(args.cycle_pause)

    final = (
        f"\n=== FIM hunt_loop jobs_done={jobs_done} ok={jobs_ok} err={jobs_err} "
        f"emails={emails_total} completed={len(completed)} "
        f"state={state_path} ===\n"
        f"RESULTADOS: {file_log.results_jsonl}\n"
        f"ERROS:      {file_log.errors_jsonl}\n"
        f"RUN LOG:    {file_log.run_txt}\n"
    )
    file_log.summary(final)


if __name__ == "__main__":
    asyncio.run(main())
