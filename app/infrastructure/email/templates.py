"""Seleção de templates HTML existentes — IA NÃO modifica o conteúdo."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.providers.registry import NICHE_TEMPLATES, get_provider_registry

logger = get_logger(__name__)

# Assuntos por nicho — tom direto, com emoji leve.
# Várias opções por nicho: cada envio sorteia uma (sem nome de contato/empresa).
SUBJECTS: dict[str, list[str]] = {
    "advogado": [
        "⚖️ Como escritórios estão digitalizando o atendimento",
        "📌 Menos burocracia no dia a dia do escritório",
        "💼 Uma ideia rápida para o seu escritório de advocacia",
        "✨ Tecnologia simples para quem vive de prazos",
    ],
    "agencia_marketing": [
        "🚀 Algo que agências de marketing estão usando agora",
        "📈 Como escalar entrega sem aumentar headcount",
        "💡 Uma ideia prática para a sua agência",
        "✨ Performance e operação — sem fricção extra",
    ],
    "empresa_ti": [
        "⚙️ Uma conversa rápida sobre operação de TI",
        "💻 Como times de TI estão cortando retrabalho",
        "🔧 Ferramenta que encaixa no stack de quem entrega projeto",
        "🚀 Ideia curta para empresas de tecnologia",
    ],
    "prestador_servico": [
        "🛠️ Mais organização para quem presta serviço",
        "📋 Atendimento e agenda sem planilha infinita",
        "✨ Uma ideia prática pro seu negócio de serviços",
        "💼 Como prestadores estão fechando mais com menos caos",
    ],
    "grupo_midiatico": [
        "📺 Conteúdo e audiência com menos gargalo operacional",
        "🎙️ Uma ideia para redações e grupos de mídia",
        "📰 Como veículos estão acelerando a produção",
        "✨ Operação de mídia sem atrito desnecessário",
    ],
    # politico e partido → canonicalize para politico
    "politico": [
        "🗳️ Campanha organizada, equipe alinhada",
        "📣 Comunicação política com mais controle",
        "🇧🇷 Uma ideia prática para equipes de campanha",
        "✨ Menos caos operacional na campanha",
    ],
    "generalista": [
        "✨ Sob medida para o seu negócio",
        "🌐 Apareça no digital",
        "🚀 Site, landing e SEO sem complicação",
        "📌 Seu negócio encontrado no Google",
        "💡 Presença digital que gera contato",
    ],
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

    def subject_for(self, niche: str, *, seed: str | None = None) -> str:
        """Assunto do nicho (emoji + variação). Sem nome de contato/empresa.

        Se `seed` for passado (ex.: id do item), a escolha é estável para
        o mesmo seed; senão sorteia a cada chamada.
        """
        from app.providers.registry import canonicalize_niche

        niche = canonicalize_niche(niche)
        options = SUBJECTS.get(niche)
        if options:
            if seed:
                idx = int(hashlib.sha256(f"{niche}:{seed}".encode()).hexdigest(), 16)
                return options[idx % len(options)]
            return random.choice(options)
        label = niche.replace("_", " ").strip() or "seu negócio"
        return f"✨ Soluções para {label}"
