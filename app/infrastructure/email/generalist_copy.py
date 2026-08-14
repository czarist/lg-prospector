"""Cópia do e-mail generalista: assunto + HTML com frase personalizada."""

from __future__ import annotations

import hashlib
import html
import random
from pathlib import Path

from app.core.config import get_settings
from app.providers.opportunity import OpportunityReport

TEMPLATE_FILE = "email-prospeccao-generalista.html"

# Assuntos genéricos — sem nome de empresa. Emoji na frente.
SUBJECTS = [
    "✨ Sob medida para o seu negócio",
    "🌐 Apareça no digital",
    "🚀 Site, landing e SEO sem complicação",
    "📌 Seu negócio encontrado no Google",
    "💡 Presença digital que gera contato",
]


def subject_for(company_name: str = "", *, seed: str | None = None) -> str:
    """Assunto genérico (emoji + variação). Ignora o nome da empresa.

    Com `seed` (id do contato/item) a escolha é estável; senão sorteia.
    """
    del company_name  # não entra no assunto
    if seed:
        idx = int(hashlib.sha256(f"generalista:{seed}".encode()).hexdigest(), 16)
        return SUBJECTS[idx % len(SUBJECTS)]
    return random.choice(SUBJECTS)


def render_html(
    *,
    company_name: str,
    report: OpportunityReport | None = None,
    personalized_line: str = "",
    templates_dir: Path | None = None,
) -> tuple[str, str, str]:
    """Retorna (filename, html, content_hash)."""
    settings = get_settings()
    directory = templates_dir or settings.templates_path
    path = directory / TEMPLATE_FILE
    template = path.read_text(encoding="utf-8")

    line = (personalized_line or (report.personalized_line if report else "") or "").strip()
    if not line:
        line = "Site institucional, landing page, template ou SEO — o essencial para o cliente te encontrar."
    empresa = html.escape((company_name or "sua empresa").strip())
    frase = html.escape(line)

    rendered = (
        template.replace("{{EMPRESA}}", empresa)
        .replace("{{FRASE_PERSONALIZADA}}", frase)
        .replace("{{UNSUBSCRIBE_URL}}", "#")
    )
    content_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return TEMPLATE_FILE, rendered, content_hash
