"""Serviço de Account (Company) no EspoCRM."""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient

logger = get_logger(__name__)


class CompanyService:
    ENTITY = "Account"

    def __init__(self, client: CRMClient) -> None:
        self.client = client

    async def create(
        self,
        name: str,
        *,
        website: str = "",
        phone: str = "",
        email: str = "",
        city: str = "",
        state: str = "",
        industry: str = "",
        description: str = "",
        account_type: str = "Customer",
    ) -> dict[str, Any]:
        from app.infrastructure.crm.sync import sanitize_phone

        payload: dict[str, Any] = {"name": name[:255]}
        if account_type:
            payload["type"] = account_type
        if website:
            payload["website"] = website[:255]
        phone_ok = sanitize_phone(phone)
        if phone_ok:
            payload["phoneNumber"] = phone_ok
        if email:
            payload["emailAddress"] = email
        if city:
            payload["billingAddressCity"] = city[:100]
        if state:
            payload["billingAddressState"] = state[:50]
        # industry só se for valor "seguro" — nichos custom (advogado etc.) quebram enum Espo
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
        if description:
            payload["description"] = description[:10000]

        result = await self.client.create(self.ENTITY, payload)
        logger.info("crm_company_created", id=result.get("id"), name=name)
        return result

    async def find_by_name(self, name: str) -> Optional[dict[str, Any]]:
        return await self.client.find_one(self.ENTITY, "name", name)

    async def get_or_create(self, name: str, **kwargs: Any) -> dict[str, Any]:
        existing = await self.find_by_name(name)
        if existing:
            return existing
        return await self.create(name, **kwargs)

    async def update(self, company_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.client.update(self.ENTITY, company_id, data)
