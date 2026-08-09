"""Serviço de atividades (Call, Meeting, Task, Email) no EspoCRM."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient

logger = get_logger(__name__)


class ActivityService:
    def __init__(self, client: CRMClient) -> None:
        self.client = client

    async def create_task(
        self,
        name: str,
        *,
        description: str = "",
        parent_type: str = "",
        parent_id: str = "",
        status: str = "Completed",
        date_end: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "status": status,
            "dateEnd": date_end or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if description:
            payload["description"] = description
        if parent_type and parent_id:
            payload["parentType"] = parent_type
            payload["parentId"] = parent_id

        result = await self.client.create("Task", payload)
        logger.info("crm_task_created", id=result.get("id"), name=name)
        return result

    async def log_email_sent(
        self,
        subject: str,
        *,
        to: str = "",
        body: str = "",
        parent_type: str = "Lead",
        parent_id: str = "",
    ) -> dict[str, Any]:
        """Registra envio como Task/Note no CRM (sem depender de OutboundEmail)."""
        description = f"E-mail enviado para {to}\nAssunto: {subject}"
        if body:
            description += f"\n\n(Template aplicado sem modificação)"
        return await self.create_task(
            name=f"E-mail prospecção: {subject[:80]}",
            description=description,
            parent_type=parent_type,
            parent_id=parent_id,
            status="Completed",
        )

    async def create_note(
        self,
        post: str,
        *,
        parent_type: str = "Lead",
        parent_id: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"post": post, "type": "Post"}
        if parent_type and parent_id:
            payload["parentType"] = parent_type
            payload["parentId"] = parent_id
        result = await self.client.create("Note", payload)
        logger.info("crm_note_created", id=result.get("id"))
        return result
