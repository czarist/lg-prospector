#!/usr/bin/env python3
"""Loop contínuo de prospecção: 5 leads × nicho (Brasil inteiro por padrão).

Só prospecta (discover → enrich → crm). O disparo de e-mail é o mailman:
  python scripts/mailman.py

Padrão: uma busca nacional (Brasil) por nicho. Escada de cidades:
  python scripts/hunt_loop.py --no-nationwide
  1. Capitais (por população; Porto Alegre primeiro só com --focus-rs)
  2. Polos nacionais + gaúchos
  3. Tier 3 só com --max-tier 3

Estado persistido em logs/hunt_loop_state.json (retoma após restart).

Exemplos:
  # todo o país (default), 5 por nicho
  python scripts/hunt_loop.py -n 5

  # escada nacional de cidades
  python scripts/hunt_loop.py --no-nationwide --max-tier 2 -n 5

  # só RS, uma volta e para
  python scripts/hunt_loop.py --only-rs --once

  # dry-run: só imprime a fila
  python scripts/hunt_loop.py --plan-only

Cada ciclo busca de novo e ignora empresa já cadastrada. Bater a cota N
não encerra o nicho×cidade — a próxima volta usa outra query.

--skip-completed volta ao modo escada (uma vez por nicho×cidade).
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
from app.core.live import write_live
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.domain.cities import (
    DEFAULT_NICHES,
    CityTarget,
    pick_query,
    resolve_hunt_cities,
)
from app.domain.stages import ItemStageStatus
from app.infrastructure.database.session import async_session_factory, init_db, reset_engine
from app.providers.http_tools import serper_block_info
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService

logger = get_logger(__name__)

def _state_path() -> Path:
    return logs_dir() / "hunt_loop_state.json"


def _hunt_log_dir() -> Path:
    return logs_dir() / "hunt"


DEFAULT_STAGES = ["discover", "enrich", "crm"]

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[hunt_loop] sinal {signum} — finalizando após job atual…", flush=True)


class HuntFileLogger:
    """Dois arquivos por dia: results + errors (JSONL) e espelho texto legível."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or _hunt_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = run_id
        self._day: str | None = None
        self.run_txt = self.log_dir / f"run_{run_id}.log"
        # cabeçalho da corrida
        header = (
            f"\n{'='*72}\n"
            f"HUNT RUN {run_id} started {datetime.now(timezone.utc).isoformat()}\n"
            f"{'='*72}\n"
        )
        self.run_txt.write_text(header, encoding="utf-8")
        self._rotate_if_needed(write_header=True)

    def _rotate_if_needed(self, *, write_header: bool = False) -> None:
        """Troca results/errors no virar do dia — o processo vive vários dias."""
        day = datetime.now().strftime("%Y%m%d")
        if self._day == day:
            return
        prev = self._day
        self._day = day
        self.results_jsonl = self.log_dir / f"results_{day}.jsonl"
        self.errors_jsonl = self.log_dir / f"errors_{day}.jsonl"
        self.results_txt = self.log_dir / f"results_{day}.log"
        self.errors_txt = self.log_dir / f"errors_{day}.log"
        if write_header or prev:
            note = (
                f"\n{'='*72}\n"
                f"HUNT RUN {self.run_id} logs {day}"
                + (f" (rotate {prev} → {day})" if prev else "")
                + f" {datetime.now(timezone.utc).isoformat()}\n"
                f"{'='*72}\n"
            )
            with self.results_txt.open("a", encoding="utf-8") as f:
                f.write(note)
            with self.errors_txt.open("a", encoding="utf-8") as f:
                f.write(note)
            if prev:
                self._append_txt(self.run_txt, f"rotated results {prev} → {day}")

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
        self._rotate_if_needed()
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
        self._rotate_if_needed()
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
        self._rotate_if_needed()
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


async def _discover_new_leads(
    stage_svc: StageService,
    *,
    campaign_id: str,
    niche: str,
    city: CityTarget,
    query_round: int,
    echo,
) -> dict[str, Any]:
    """Descoberta com retry: se a query só devolver lead já no CRM, gira e tenta de novo."""
    max_rounds = 4
    created_total = 0
    queued_total = 0
    last: dict[str, Any] = {}
    last_query = ""
    attempts = 0
    for attempt in range(max_rounds):
        attempts = attempt + 1
        if attempt > 0:
            camp = await stage_svc.get_campaign(campaign_id)
            if camp:
                next_q = pick_query(niche, city.city, query_round + attempt)
                camp.query = next_q
                last_query = next_q
                await stage_svc.session.flush()
                echo(f"  … discover retry {attempts}/{max_rounds} query={next_q!r}")
        last = await stage_svc.run_stage(campaign_id, "discover")
        n = int(last.get("companies_found") or 0)
        queued = int(last.get("queued") or 0)
        created_total += n
        queued_total += queued
        last_query = str(last.get("query_used") or last_query)
        if n > 0 or queued > 0:
            break
        logger.info(
            "nicho_discover_retry",
            niche=niche,
            city=city.city,
            state=city.state,
            attempt=attempts,
            query=last_query,
        )
    last["companies_found"] = created_total
    last["queued"] = queued_total
    last["discover_attempts"] = attempts
    blocked = serper_block_info()
    if blocked.get("blocked"):
        echo("  ⚠ Serper sem crédito — busca grátis (DDG/OSM) até recarregar")
        last["search_fallback"] = "free"
    return last


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
    global _stop
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
                "query_round": query_round,
                "discover_round": query_round,
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
            write_live("nicho", {"phase": st, "detail": f"etapa {st}"})
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
                    if isinstance(result, dict) and result.get("provider_blocked"):
                        _echo(
                            "  ✗ SMTP bloqueado (Zoho 550 unusual activity). "
                            "Parando o hunt para não marcar lead como failed."
                        )
                        _stop = True
                elif st == "discover":
                    result = await _discover_new_leads(
                        stage_svc,
                        campaign_id=cid,
                        niche=niche,
                        city=city,
                        query_round=query_round,
                        echo=_echo,
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
            if isinstance(result, dict):
                if st == "discover":
                    detail = (
                        f"achou {result.get('companies_found', 0)} "
                        f"fila {result.get('queued', 0)} "
                        f"tent={result.get('discover_attempts', 1)}"
                    )
                elif st == "enrich":
                    detail = (
                        f"+{result.get('enriched', 0)} e-mail  "
                        f"desc {result.get('discarded', 0)}"
                    )
                elif st == "crm":
                    detail = f"crm {result.get('synced', 0)}  falha {result.get('failed', 0)}"
                else:
                    detail = f"etapa {st}"
                write_live("nicho", {"phase": st, "detail": detail})
            if file_log:
                file_log.stage(
                    niche=niche,
                    city=city.city,
                    state=city.state,
                    stage=st,
                    result=result if isinstance(result, dict) else {"raw": str(result)},
                )

            if st in {"enrich", "crm"}:
                drain = (
                    await stage_svc.enrich_ready(
                        niche=niche, limit=max_results, exclude_campaign_id=cid
                    )
                    if st == "enrich"
                    else await stage_svc.crm_ready(
                        niche=niche, limit=max_results, exclude_campaign_id=cid
                    )
                )
                if isinstance(result, dict) and isinstance(drain, dict):
                    for k in ("enriched", "discarded", "synced", "failed"):
                        if k in drain:
                            result[k] = int(result.get(k) or 0) + int(drain.get(k) or 0)
                    result["drained"] = drain.get("seen") or 0
                    stage_results[st] = result
                    _echo(f"  drenou {st}: {drain}")

            if st == "enrich" and result.get("need_more_discover"):
                queued_now = int((stage_results.get("discover") or {}).get("queued") or 0)
                if queued_now > 0:
                    _echo("  … enrich vazio: candidatos na fila do reviewer (sem reforço SERP)")
                else:
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
                            _echo(
                                f"  ✗ reforço discover/enrich falhou (seguindo com o que há): {reforco_exc}"
                            )
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
    queued = int((stage_results.get("discover") or {}).get("queued") or 0)
    enriched_n = int((stage_results.get("enrich") or {}).get("enriched") or 0)
    good = max(good, enriched_n)
    dispatch_info = stage_results.get("dispatch") or {}
    return {
        "event": "job_done",
        "campaign_id": cid,
        "niche": niche,
        "city": city.city,
        "state": city.state,
        "query": query,
        "good": good,
        "queued": queued,
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
        "ok": good >= max(1, max_results // 2) or queued > 0,
        "full": good >= max_results,
    }


async def main() -> None:
    global _stop

    p = argparse.ArgumentParser(description="Loop de caçada multi-nicho / multi-cidade")
    p.add_argument("-n", "--max", type=int, default=5, help="Leads por nicho×alvo")
    p.add_argument(
        "--niches",
        default=",".join(DEFAULT_NICHES),
        help="Nichos csv (default: todos)",
    )
    p.add_argument(
        "--nationwide",
        dest="nationwide",
        action="store_true",
        help="Busca em todo o Brasil (default)",
    )
    p.add_argument(
        "--no-nationwide",
        dest="nationwide",
        action="store_false",
        help="Usa escada de cidades em vez de uma busca nacional",
    )
    p.add_argument(
        "--focus-rs",
        action="store_true",
        help="Escada de cidades com RS primeiro (desliga busca nacional)",
    )
    p.add_argument("--no-focus-rs", action="store_true", help="Sem prioridade RS (já é o default)")
    p.set_defaults(nationwide=True)
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
        default="discover,enrich,crm",
        help="Etapas csv (default: discover,enrich,crm — disparo é o mailman)",
    )
    p.add_argument(
        "--dispatch",
        action="store_true",
        help="Inclui etapa dispatch neste hunt (legado; prefira scripts/mailman.py)",
    )
    p.add_argument(
        "--dry-run-dispatch",
        action="store_true",
        help="Monta e-mails mas não envia via SMTP (só com --dispatch)",
    )
    p.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Garante que dispatch não entra (já é o default)",
    )
    p.add_argument(
        "--cooldown-days",
        type=int,
        default=4,
        help="Não reenviar pro mesmo contato em menos de N dias (default 4)",
    )
    p.add_argument("--pause", type=float, default=3.0, help="Pausa entre jobs (s)")
    p.add_argument("--cycle-pause", type=float, default=20.0, help="Pausa entre voltas (s)")
    p.add_argument("--once", action="store_true", help="Uma volta na fila e encerra")
    p.add_argument("--max-jobs", type=int, default=0, help="Para após N jobs (0=∞)")
    p.add_argument(
        "--skip-completed",
        action="store_true",
        default=False,
        help="Modo escada: pula niche×cidade já feito neste state",
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
    p.add_argument(
        "--max-partial-attempts",
        type=int,
        default=5,
        help=(
            "Após N tentativas parciais (good < meta ok) no mesmo job, desiste e "
            "marca completed/skipped (0=ilimitado; default 5)"
        ),
    )
    p.add_argument("--plan-only", action="store_true", help="Só lista a fila e sai")
    p.add_argument("--state-file", default=str(_state_path()))
    p.add_argument(
        "--log-dir",
        default=str(_hunt_log_dir()),
        help="Pasta dos logs results/errors (default LOGS_DIR/hunt)",
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

    focus_rs = bool(args.focus_rs) and not bool(args.no_focus_rs)
    niches = [n.strip() for n in args.niches.split(",") if n.strip()]
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if args.no_dispatch:
        stages = [s for s in stages if s != "dispatch"]
    elif args.dispatch and "dispatch" not in stages:
        stages.append("dispatch")
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()] or None
    city_limit = args.city_limit or None

    cities = resolve_hunt_cities(
        nationwide=args.nationwide,
        focus_rs=focus_rs,
        only_rs=args.only_rs,
        only_capitals=args.only_capitals,
        max_tier=args.max_tier,
        min_population_k=args.min_pop_k,
        limit=city_limit,
        include_states=states,
    )
    jobs = _build_jobs(cities, niches, city_first=not args.niche_first)

    print("=== HUNT LOOP ===", flush=True)
    print(
        f"backend={settings.search_backend} serper={'yes' if settings.serper_api_key else 'no'} "
        f"llm={settings.hunt_use_llm} model={settings.model}  "
        f"review_queue={settings.review_queue_enabled}  "
        f"(sempre lead NOVO; Qwen é scripts/reviewer.py)",
        flush=True,
    )
    print(
        f"niches={niches} | cidades={len(cities)} | jobs/volta={len(jobs)} | n={args.max}",
        flush=True,
    )
    print(
        f"stages={stages} | cooldown={args.cooldown_days}d | "
        f"dry_run_dispatch={args.dry_run_dispatch} | "
        f"max_partial_attempts={args.max_partial_attempts}",
        flush=True,
    )
    if "dispatch" not in stages:
        print("disparo: scripts/mailman.py (este hunt só prospecta)", flush=True)
        print("validação IA: scripts/reviewer.py (fila Redis → grava ou descarta)", flush=True)
    print(
        f"nationwide={len(cities) == 1 and cities[0].nationwide} "
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

    file_log = HuntFileLogger(Path(args.log_dir) if args.log_dir else _hunt_log_dir())
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
    visits: dict = state.setdefault("visits", {})

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
                # reprocessa incompletos só se --require-full e ainda não desistiu
                if (
                    args.require_full
                    and not prev.get("full")
                    and not prev.get("skipped")
                ):
                    pass
                else:
                    continue

            visit_n = int(visits.get(key) or 0)
            header = (
                f"\n--- [{idx+1}/{len(jobs)}] {niche} @ {city.label} "
                f"(T{city.tier} pop~{city.population_k}k) visita={visit_n+1} ---"
            )
            print(header, flush=True)
            file_log.log_console(header)
            write_live(
                "nicho",
                {
                    "status": "running",
                    "cycle": cycle,
                    "index": idx + 1,
                    "total": len(jobs),
                    "niche": niche,
                    "city": city.label,
                    "phase": "job",
                    "detail": f"visita {visit_n + 1}",
                    "job_started_at": datetime.now(timezone.utc).isoformat(),
                    "ok": jobs_ok,
                    "err": jobs_err,
                },
            )
            t0 = time.monotonic()
            try:
                result = await _run_one(
                    niche=niche,
                    city=city,
                    max_results=args.max,
                    stages=stages,
                    query_round=visit_n,
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
                        f"fila={result.get('queued', 0)} "
                        f"emails={result.get('emails_sent', 0)} "
                        f"cooldown_skip={result.get('emails_cooldown', 0)} "
                        f"full={result.get('full')} em {elapsed:.0f}s"
                    )
                    print(msg, flush=True)
                    file_log.log_console(msg)
                else:
                    # parcial fraco: conta tentativas e desiste após o teto
                    prev_fail = failed.get(key) or {}
                    attempts = int(prev_fail.get("attempts") or 0) + 1
                    zero_streak = int(prev_fail.get("zero_good_streak") or 0)
                    if int(result.get("good") or 0) == 0:
                        zero_streak += 1
                    else:
                        zero_streak = 0
                    best_good = max(
                        int(prev_fail.get("best_good") or 0),
                        int(result.get("good") or 0),
                    )
                    failed[key] = {
                        "good": result["good"],
                        "attempts": attempts,
                        "zero_good_streak": zero_streak,
                        "best_good": best_good,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "items": result.get("items_by_stage"),
                        "campaign_id": result.get("campaign_id"),
                    }
                    max_att = int(args.max_partial_attempts or 0)
                    give_up = max_att > 0 and attempts >= max_att

                    file_log.error(
                        {
                            "event": "job_partial",
                            "niche": niche,
                            "city": city.city,
                            "state": city.state,
                            "campaign_id": result.get("campaign_id"),
                            "good": result.get("good"),
                            "target": result.get("target"),
                            "attempts": attempts,
                            "zero_good_streak": zero_streak,
                            "best_good": best_good,
                            "max_partial_attempts": max_att or None,
                            "gave_up": give_up,
                            "items_by_stage": result.get("items_by_stage"),
                            "message": (
                                "max_partial_attempts — desistindo"
                                if give_up
                                else "meta incompleta"
                            ),
                        }
                    )

                    if give_up:
                        completed[key] = {
                            "campaign_id": result.get("campaign_id"),
                            "good": result["good"],
                            "full": False,
                            "emails_sent": result.get("emails_sent"),
                            "skipped": True,
                            "skip_reason": "max_partial_attempts",
                            "attempts": attempts,
                            "best_good": best_good,
                            "zero_good_streak": zero_streak,
                            "at": datetime.now(timezone.utc).isoformat(),
                            "cycle": cycle,
                        }
                        failed.pop(key, None)
                        msg = (
                            f"  ∅ desistindo good={result['good']}/{args.max} "
                            f"após {attempts} tentativas parciais "
                            f"(best={best_good}) — marcado skipped"
                        )
                        print(msg, flush=True)
                        file_log.log_console(msg)
                    else:
                        left = max_att - attempts if max_att > 0 else "∞"
                        msg = (
                            f"  ~ parcial good={result['good']}/{args.max} "
                            f"attempt={attempts}/{max_att or '∞'} "
                            f"(restam {left}) — re-tentará em outro ciclo"
                        )
                        print(msg, flush=True)
                        file_log.log_console(msg)

                visits[key] = visit_n + 1
                cycle_hits += 1
                jobs_done += 1
                write_live(
                    "nicho",
                    {
                        "phase": "idle",
                        "last_elapsed_s": round(elapsed, 1),
                        "last_good": result.get("good"),
                        "ok": jobs_ok,
                        "err": jobs_err,
                    },
                )
            except Exception as exc:
                jobs_err += 1
                jobs_done += 1
                logger.exception("hunt_job_failed", niche=niche, city=city.label)
                prev_fail = failed.get(key) or {}
                attempts = int(prev_fail.get("attempts") or 0) + 1
                failed[key] = {
                    "error": str(exc)[:300],
                    "attempts": attempts,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                max_att = int(args.max_partial_attempts or 0)
                give_up = max_att > 0 and attempts >= max_att
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
                        "attempts": attempts,
                        "gave_up": give_up,
                    }
                )
                if give_up:
                    completed[key] = {
                        "campaign_id": prev_fail.get("campaign_id"),
                        "good": int(prev_fail.get("best_good") or prev_fail.get("good") or 0),
                        "full": False,
                        "skipped": True,
                        "skip_reason": "max_error_attempts",
                        "attempts": attempts,
                        "last_error": str(exc)[:300],
                        "at": datetime.now(timezone.utc).isoformat(),
                        "cycle": cycle,
                    }
                    failed.pop(key, None)
                    print(
                        f"  ✗ erro (desistindo após {attempts}x): {exc}",
                        flush=True,
                    )
                else:
                    print(f"  ✗ erro: {exc}", flush=True)
                visits[key] = visit_n + 1
                write_live(
                    "nicho",
                    {"phase": "erro", "err": jobs_err, "last_error": str(exc)[:160]},
                )

            state["completed"] = completed
            state["failed"] = failed
            state["visits"] = visits
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

        if args.skip_completed:
            def _still_pending(n: str, c: CityTarget) -> bool:
                k = _job_key(n, c)
                if k not in completed:
                    return True
                meta = completed[k]
                if meta.get("skipped"):
                    return False
                if args.require_full and not meta.get("full"):
                    return True
                return False

            pending = [(n, c) for n, c in jobs if _still_pending(n, c)]
            if not pending and not args.redo:
                file_log.summary("Fila completa — nada pendente. Encerrando.")
                break
        else:
            file_log.summary(
                f"Ciclo {cycle} fechado — próxima volta busca leads novos "
                f"(ignora empresa já cadastrada)."
            )

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
