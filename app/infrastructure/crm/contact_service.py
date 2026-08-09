"""Serviço de Contact no EspoCRM."""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient

logger = get_logger(__name__)


class ContactService:
    ENTITY = "Contact"

    def __init__(self, client: CRMClient) -> None:
        self.client = client

    async def create(
        self,
        first_name: str,
        last_name: str = "",
        *,
        email: str = "",
        phone: str = "",
        account_id: str = "",
        title: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        if not last_name and " " in first_name.strip():
            parts = first_name.strip().split(None, 1)
            first_name, last_name = parts[0], parts[1]
        payload: dict[str, Any] = {
            "firstName": first_name or "Contato",
            "lastName": last_name or ".",
        }
        from app.infrastructure.crm.sync import sanitize_phone

        if email:
            payload["emailAddress"] = email
        phone_ok = sanitize_phone(phone)
        if phone_ok:
            payload["phoneNumber"] = phone_ok
        if account_id:
            payload["accountId"] = account_id
        if title:
            payload["title"] = title
        if description:
            payload["description"] = description[:10000]

        result = await self.client.create(self.ENTITY, payload)
        logger.info("crm_contact_created", id=result.get("id"), email=email)
        return result

    async def find_by_email(self, email: str) -> Optional[dict[str, Any]]:
        return await self.client.find_one(self.ENTITY, "emailAddress", email)

    async def get_or_create(
        self,
        first_name: str,
        *,
        email: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if email:
            existing = await self.find_by_email(email)
            if existing:
                return existing
        return await self.create(first_name, email=email, **kwargs)
