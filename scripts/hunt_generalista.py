#!/usr/bin/env python3
"""Loop da rotina GENERALISTA — só prospecção (cadastro).

Cada ciclo descobre empresas na cidade da vez, analisa e cadastra no CRM.
O disparo de e-mail ficou no mailman (`scripts/mailman.py`).

Uso:
  python scripts/hunt_generalista.py -n 8
  python scripts/hunt_generalista.py --no-nationwide --max-tier 3 -n 8
  python scripts/hunt_generalista.py --city "Porto Alegre" --state RS --once
  python scripts/hunt_generalista.py --with-send --once   # legado: também dispara
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.live import write_live
from app.core.logging import get_logger, setup_logging
from app.core.paths import logs_dir
from app.domain.cities import CityTarget, resolve_hunt_cities
from app.providers.http_tools import serper_block_info
from app.infrastructure.database.session import (
    async_session_factory,
    dispose_db,
    init_db,
    reset_engine,
)
from app.services.generalist_service import GeneralistService

logger = get_logger(__name__)

def _state_path() -> Path:
    return logs_dir() / "hunt_generalista_state.json"


def _log_path() -> Path:
    return logs_dir() / "generalista.jsonl"

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n[generalista] sinal {signum} — finalizando após o ciclo atual…", flush=True)


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _log(row: dict[str, Any]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


async def _cycle(
    *,
    city: CityTarget,
    max_new: int,
    max_send: int,
    wait_days: int,
    dry_run: bool,
    skip_existing: bool,
    skip_followup: bool,
    skip_discover: bool,
    query: str,
    query_round: int = 0,
) -> dict[str, Any]:
    reset_engine()
    await init_db()
    factory = async_session_factory()
    async with factory() as session:
        svc = GeneralistService(session)
        result = await svc.run(
            city=city.city,
            state=city.state,
            query=query,
            max_new=max_new,
            max_send_existing=max_send,
            max_send_followup=max_send,
            wait_days=wait_days,
            dry_run=dry_run,
            skip_existing=skip_existing,
            skip_followup=skip_followup,
            skip_discover=skip_discover,
            query_round=query_round,
        )
        await session.commit()
    await dispose_db()
    return result


def _print_metrics(result: dict[str, Any]) -> None:
    e1 = result.get("etapa1_crm_existente") or {}
    e2 = result.get("etapa2_followup") or {}
    e3 = result.get("etapa3_descoberta") or {}
    print("  — etapa 1 CRM existente:", flush=True)
    print(f"      enviados={e1.get('enviados', 0)} falhas={e1.get('falhas', 0)} "
          f"pulados={e1.get('pulados', 0)}", flush=True)
    print("  — etapa 2 follow-up 4d:", flush=True)
    print(f"      enviados={e2.get('enviados', 0)} falhas={e2.get('falhas', 0)} "
          f"maduros={e2.get('maduros', 0)}", flush=True)
    print("  — etapa 3 descoberta (sem envio):", flush=True)
    print(
        f"      pesquisadas={e3.get('pesquisadas', 0)} "
        f"enriquecidas={e3.get('enriquecidas', 0)} "
        f"cadastradas={e3.get('cadastradas_crm', 0)} "
        f"descartadas={e3.get('descartadas', 0)} "
        f"tentativas={e3.get('discover_attempts', 1)}",
        flush=True,
    )
    blocked = serper_block_info()
    if blocked.get("blocked"):
        print(
            "  ⚠ Serper sem crédito — busca grátis (DDG/OSM) até recarregar",
            flush=True,
        )


async def main() -> None:
    p = argparse.ArgumentParser(description="Rotina generalista (independente dos nichos)")
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
    p.add_argument("--focus-rs", action="store_true", help="Escada de cidades com RS primeiro")
    p.add_argument("--only-rs", action="store_true", help="Só cidades do RS")
    p.add_argument("--max-tier", type=int, default=3)
    p.add_argument("--min-pop-k", type=int, default=50)
    p.set_defaults(nationwide=True)
    p.add_argument("-n", "--max", type=int, default=8, help="Novos leads por alvo")
    p.add_argument(
        "--with-send",
        action="store_true",
        help="Também dispara e-mail (legado; prefira scripts/mailman.py)",
    )
    p.add_argument("--max-send", type=int, default=15, help="Teto de envios se --with-send")
    p.add_argument("--wait-days", type=int, default=4, help="Espera só para lead generalista novo")
    p.add_argument("--city", default="", help="Trava numa cidade (com --once)")
    p.add_argument("--state", default="", help="UF da cidade travada")
    p.add_argument("--query", default="", help="Query extra de busca")
    p.add_argument("--once", action="store_true", help="Um ciclo e para")
    p.add_argument("--dry-run", action="store_true", help="Não envia SMTP de verdade")
    p.add_argument("--skip-existing", action="store_true", help="Pula etapa 1 (CRM existente)")
    p.add_argument("--skip-followup", action="store_true", help="Pula etapa 2 (follow-up 4d)")
    p.add_argument("--skip-discover", action="store_true", help="Pula etapa 3 (descoberta)")
    p.add_argument("--pause", type=float, default=3.0, help="Pausa entre cidades")
    p.add_argument("--cycle-pause", type=float, default=20.0, help="Pausa entre voltas da fila")
    args = p.parse_args()
    if not args.with_send and args.skip_discover:
        print(
            "Nada a fazer: discover está off e o disparo agora é scripts/mailman.py "
            "(use --with-send só se quiser o envio legado neste script).",
            flush=True,
        )
        raise SystemExit(2)

    setup_logging()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cities = resolve_hunt_cities(
        nationwide=args.nationwide,
        focus_rs=args.focus_rs,
        only_rs=args.only_rs,
        max_tier=args.max_tier,
        min_population_k=args.min_pop_k,
        city=args.city,
        state=args.state,
    )
    if not cities:
        print("Fila de cidades vazia.", flush=True)
        raise SystemExit(1)

    state = _load_state()
    idx = int(state.get("city_index") or 0) % len(cities)
    cycle = int(state.get("cycle") or 0)

    from app.core.config import get_settings

    settings = get_settings()
    print(
        f"Generalista: {len(cities)} alvo(s)  idx={idx}  "
        f"wait={args.wait_days}d  dry_run={args.dry_run}  "
        f"send={'on' if args.with_send else 'mailman'}  "
        f"backend={settings.search_backend} serper={'yes' if settings.serper_api_key else 'no'}  "
        f"(sempre descobre lead NOVO; Serper sem crédito → DDG/OSM)",
        flush=True,
    )

    while not _stop:
        city = cities[idx]
        print(f"\n=== ciclo {cycle}  {city.label} ===", flush=True)
        write_live(
            "generalista",
            {
                "status": "running",
                "cycle": cycle,
                "index": idx + 1,
                "total": len(cities),
                "city": city.label,
                "phase": "descoberta",
                "job_started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        try:
            result = await _cycle(
                city=city,
                max_new=args.max,
                max_send=args.max_send,
                wait_days=args.wait_days,
                dry_run=args.dry_run,
                skip_existing=args.skip_existing if args.with_send else True,
                skip_followup=args.skip_followup if args.with_send else True,
                skip_discover=args.skip_discover,
                query=args.query,
                query_round=cycle * len(cities) + idx,
            )
        except Exception as exc:
            logger.exception("generalista_cycle_error", city=city.label)
            print(f"  ERRO: {exc}", flush=True)
            _log({"event": "error", "city": city.label, "error": str(exc)})
            write_live("generalista", {"phase": "erro", "last_error": str(exc)[:160]})
            result = {"error": str(exc)}
        else:
            _print_metrics(result)
            _log({"event": "cycle", "city": city.label, "result": result})
            e3 = (result.get("etapa3_descoberta") or {})
            write_live(
                "generalista",
                {
                    "phase": "idle",
                    "last_new": e3.get("cadastradas_crm"),
                    "last_found": e3.get("pesquisadas"),
                    "last_enriched": e3.get("enriquecidas"),
                    "last_discarded": e3.get("descartadas"),
                    "last_city": city.label,
                    "detail": (
                        f"achou {e3.get('pesquisadas', 0)}  "
                        f"crm {e3.get('cadastradas_crm', 0)}"
                    ),
                },
            )
            if result.get("smtp_provider_block") or (
                (result.get("etapa1_crm_existente") or {}).get("provider_blocked")
                or (result.get("etapa2_followup") or {}).get("provider_blocked")
            ):
                print(
                    "  ✗ SMTP bloqueado (Zoho 550). Encerrando generalista.",
                    flush=True,
                )
                break

        idx = (idx + 1) % len(cities)
        if idx == 0:
            cycle += 1
        state = {
            "city_index": idx,
            "cycle": cycle,
            "last_city": city.label,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_state(state)

        if args.once or _stop:
            break
        pause = args.cycle_pause if idx == 0 else args.pause
        if pause > 0:
            await asyncio.sleep(pause)

    print("\nGeneralista encerrado.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
