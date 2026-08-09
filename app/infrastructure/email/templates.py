"""Seleção de templates HTML existentes — IA NÃO modifica o conteúdo."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.providers.registry import NICHE_TEMPLATES, get_provider_registry

logger = get_logger(__name__)

# Subjects padrão por nicho (sem nome do contato/empresa)
SUBJECTS: dict[str, str] = {
    "advogado": "Soluções para escritórios de advocacia",
    "agencia_marketing": "Soluções para agências de marketing",
    "empresa_ti": "Soluções para empresas de TI",
    "prestador_servico": "Soluções para prestadores de serviço",
    "grupo_midiatico": "Soluções para grupos de mídia",
    # politico e partido são o mesmo nicho (canonicalize → politico)
    "politico": "Soluções para equipes de campanha",
}


class TemplateSelector:
    """
    Seleciona o arquivo HTML correspondente ao nicho.
    A IA NÃO escreve e-mails e NÃO modifica o HTML.
    """

    def __init__(self, templates_dir: Path | None = None) -> None:
        settings = get_settings()
        self.templates_dir = templates_dir or settings.templates_path

    def select(self, niche: str) -> str:
        """Retorna o nome do arquivo de template para o nicho."""
        from app.providers.registry import canonicalize_niche

        niche = canonicalize_niche(niche)
        registry = get_provider_registry()
        try:
            return registry.template_for_niche(niche)
        except KeyError:
            if niche in NICHE_TEMPLATES:
                return NICHE_TEMPLATES[niche]
            raise FileNotFoundError(f"Nenhum template mapeado para nicho: {niche}") from None

    def load(self, niche: str) -> tuple[str, str, str]:
        """
        Carrega o HTML exatamente como está no disco.

        Returns:
            (template_name, html_content, content_hash)
        """
        from app.providers.registry import canonicalize_niche

        niche = canonicalize_niche(niche)
        filename = self.select(niche)
        path = self.templates_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Template não encontrado: {path}")

        # Leitura binária → decode: conteúdo intacto, sem transformação
        raw = path.read_bytes()
        html = raw.decode("utf-8")
        content_hash = hashlib.sha256(raw).hexdigest()

        logger.info(
            "template_selected",
            niche=niche,
            file=filename,
            hash=content_hash[:12],
            bytes=len(raw),
        )
        return filename, html, content_hash

    def subject_for(self, niche: str) -> str:
        """Assunto fixo do nicho — sem nome de contato ou empresa."""
        from app.providers.registry import canonicalize_niche

        niche = canonicalize_niche(niche)
        if niche in SUBJECTS:
            return SUBJECTS[niche]
        # fallback legível: "Soluções para <nicho>"
        label = niche.replace("_", " ").strip() or "seu negócio"
        return f"Soluções para {label}"
