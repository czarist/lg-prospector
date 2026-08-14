"""Caminhos do projeto. Logs: default no repo; LOGS_DIR no .env aponta pra outro disco."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def logs_dir() -> Path:
    """Pasta de logs. Relativo = dentro do projeto; absoluto = caminho do env.

    Se o caminho absoluto não for gravável (HD ainda sem permissão),
    cai no default do projeto.
    """
    from app.core.config import get_settings

    raw = (get_settings().logs_dir or "logs").strip() or "logs"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except OSError:
        fallback = PROJECT_ROOT / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
