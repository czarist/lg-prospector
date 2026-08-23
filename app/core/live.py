"""Heartbeat mínimo dos workers — o cockpit lê estes JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.paths import logs_dir


def _live_dir() -> Path:
    path = logs_dir() / "live"
    path.mkdir(parents=True, exist_ok=True)
    return path


def live_path(name: str) -> Path:
    return _live_dir() / f"{name}.json"


def write_live(name: str, update: dict[str, Any]) -> None:
    path = live_path(name)
    prev: dict[str, Any] = {}
    if path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev.update(update)
    if "last_error" not in update and str(update.get("phase") or "") not in {
        "erro",
        "error",
    }:
        prev.pop("last_error", None)
    prev["ts"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prev, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def read_live(name: str) -> dict[str, Any]:
    path = live_path(name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
