"""Serviço de Opportunity / pipeline no EspoCRM."""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient

logger = get_logger(__name__)


class OpportunityService:
    ENTITY = "Opportunity"

    # Estágios típicos do pipeline EspoCRM
    STAGE_PROSPECTING = "Prospecting"
    STAGE_QUALIFICATION = "Qualification"
    STAGE_PROPOSAL = "Proposal"
    STAGE_NEGOTIATION = "Negotiation"
    STAGE_CLOSED_WON = "Closed Won"
    STAGE_CLOSED_LOST = "Closed Lost"

    def __init__(self, client: CRMClient) -> None:
        self.client = client

    async def create(
        self,
        name: str,
        *,
        account_id: str = "",
        stage: str = STAGE_PROSPECTING,
        amount: float | None = None,
        probability: int | None = None,
        lead_source: str = "Web Site",
        description: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "stage": stage,
            "leadSource": lead_source,
        }
        if account_id:
            payload["accountId"] = account_id
        if amount is not None:
            payload["amount"] = amount
        if probability is not None:
            payload["probability"] = probability
        if description:
            payload["description"] = description

        result = await self.client.create(self.ENTITY, payload)
        logger.info("crm_opportunity_created", id=result.get("id"), stage=stage)
        return result

    async def update_stage(self, opportunity_id: str, stage: str) -> dict[str, Any]:
        result = await self.client.update(self.ENTITY, opportunity_id, {"stage": stage})
        logger.info("crm_pipeline_updated", id=opportunity_id, stage=stage)
        return result

    async def advance_on_response(self, opportunity_id: str) -> dict[str, Any]:
        """Avança pipeline quando há resposta do lead."""
        return await self.update_stage(opportunity_id, self.STAGE_QUALIFICATION)
