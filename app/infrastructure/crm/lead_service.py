"""Serviço de Lead no EspoCRM."""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient

logger = get_logger(__name__)


class LeadService:
    ENTITY = "Lead"

    def __init__(self, client: CRMClient) -> None:
        self.client = client

    async def create(
        self,
        first_name: str,
        last_name: str = "",
        *,
        email: str = "",
        phone: str = "",
        account_name: str = "",
        website: str = "",
        source: str = "Web Site",
        status: str = "New",
        description: str = "",
        industry: str = "",
        address_city: str = "",
        address_state: str = "",
    ) -> dict[str, Any]:
        if not last_name and " " in (first_name or "").strip():
            parts = first_name.strip().split(None, 1)
            first_name, last_name = parts[0], parts[1]
        payload: dict[str, Any] = {
            "firstName": first_name or "Lead",
            "lastName": last_name or ".",
            "status": status,
            "source": source,
        }
        from app.infrastructure.crm.sync import sanitize_phone

        if email:
            payload["emailAddress"] = email
        phone_ok = sanitize_phone(phone)
        if phone_ok:
            payload["phoneNumber"] = phone_ok
        if account_name:
            payload["accountName"] = account_name[:255]
        if website:
            payload["website"] = website[:255]
        if description:
            payload["description"] = description[:10000]
        # industry enum do Espo — não enviar nichos custom
        if industry and industry in {
            "Advertising",
            "Technology",
            "Finance",
            "Education",
            "Healthcare",
            "Government",
            "Legal",
            "Other",
        }:
            payload["industry"] = industry
        if address_city:
            payload["addressCity"] = address_city[:100]
        if address_state:
            payload["addressState"] = address_state[:50]

        result = await self.client.create(self.ENTITY, payload)
        logger.info("crm_lead_created", id=result.get("id"), email=email)
        return result

    async def update_status(self, lead_id: str, status: str) -> dict[str, Any]:
        return await self.client.update(self.ENTITY, lead_id, {"status": status})

    async def get(self, lead_id: str) -> dict[str, Any]:
        return await self.client.get(self.ENTITY, lead_id)

    async def find_by_email(self, email: str) -> Optional[dict[str, Any]]:
        return await self.client.find_one(self.ENTITY, "emailAddress", email)
