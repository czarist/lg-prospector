"""Etapas da campanha — pesquisa e disparo segmentados."""

from __future__ import annotations

from enum import Enum


class CampaignStage(str, Enum):
    """Pipeline em etapas independentes (podem ser chamadas separadamente)."""

    CREATED = "created"  # campanha criada, nada rodou
    DISCOVER = "discover"  # busca empresas na web
    ENRICH = "enrich"  # acha e-mail do domínio do site
    CRM = "crm"  # Account + Contact + Lead no Espo
    DISPATCH = "dispatch"  # envio de e-mails (template HTML)
    DONE = "done"

    @classmethod
    def order(cls) -> list["CampaignStage"]:
        return [
            cls.CREATED,
            cls.DISCOVER,
            cls.ENRICH,
            cls.CRM,
            cls.DISPATCH,
            cls.DONE,
        ]

    def next_stage(self) -> "CampaignStage | None":
        seq = self.order()
        try:
            i = seq.index(self)
        except ValueError:
            return None
        if i + 1 >= len(seq):
            return None
        return seq[i + 1]


# Status de item por etapa
class ItemStageStatus(str, Enum):
    PENDING = "pending"
    DISCOVERED = "discovered"  # empresa achada, sem e-mail ainda
    ENRICHED = "enriched"  # tem e-mail do domínio
    DISCARDED = "discarded"  # sem e-mail após multi-pass
    CRM_SYNCED = "crm_synced"
    QUEUED = "queued"  # na fila de disparo
    SENT = "sent"
    FAILED = "failed"
