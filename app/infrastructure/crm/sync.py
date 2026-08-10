"""Sincroniza lead prospectado no EspoCRM (Account + Contact + Lead).

Campos alinhados ao API.md — evita industry/phone inválidos que geram 400.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import get_logger
from app.infrastructure.crm.client import CRMClient
from app.infrastructure.crm.company_service import CompanyService
from app.infrastructure.crm.contact_service import ContactService
from app.infrastructure.crm.lead_service import LeadService

logger = get_logger(__name__)


def sanitize_phone(phone: str | None) -> str:
    """Espo TrentinCRM aceita phoneNumber em E.164 (+55…). Outros formatos → 400."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone.strip())
    if len(digits) < 10 or len(digits) > 13:
        return ""
    if len(set(digits[-8:])) <= 1:
        return ""
    # já com país 55
    if digits.startswith("55") and len(digits) >= 12:
        return f"+{digits}"
    # DDD + número (10 ou 11 dígitos) → +55
    if len(digits) in (10, 11):
        return f"+55{digits}"
    return ""


def split_name(full: str) -> tuple[str, str]:
    full = (full or "").strip() or "Lead"
    # remove lixo de título de página
    for sep in [" - ", " | ", " – ", " — "]:
        if sep in full:
            full = full.split(sep)[0].strip()
    parts = full.split(None, 1)
    if len(parts) == 1:
        return parts[0][:100], "."
    return parts[0][:100], parts[1][:100]


def _mismatched_email(record: dict[str, Any], email: str) -> bool:
    """Espo detecta duplicidade de Contact/Lead por NOME, não por e-mail — um
    create() pode voltar (via 409) um registro "duplicado" de outra pessoa/cidade
    cujo nome bateu mas o e-mail é outro (comum em nomes institucionais genéricos
    como "Gabinete do Prefeito"). Isso é o que causa contact_id/lead_id local
    apontando pro registro errado no CRM."""
    returned = (record.get("emailAddress") or "").strip().lower()
    return bool(returned) and returned != (email or "").strip().lower()


def _disambiguated_last_name(last: str, city: str, email: str) -> str:
    tag = city or email.rsplit("@", 1)[-1]
    return f"{(last or '.').strip()} ({tag})"[:100]


@dataclass
class CRMSyncResult:
    account_id: Optional[str] = None
    contact_id: Optional[str] = None
    lead_id: Optional[str] = None
    errors: list[str] | None = None

    @property
    def ok(self) -> bool:
        return bool(self.lead_id and self.contact_id)


class CRMSyncService:
    """
    Para cada lead com e-mail:
    1. Account (empresa) — get_or_create por nome
    2. Contact — get_or_create por e-mail, link accountId
    3. Lead — get_or_create por e-mail
    """

    def __init__(self, client: CRMClient | None = None) -> None:
        self.client = client or CRMClient()
        self.companies = CompanyService(self.client)
        self.contacts = ContactService(self.client)
        self.leads = LeadService(self.client)

    async def sync_prospect(
        self,
        *,
        company_name: str,
        contact_name: str = "",
        email: str,
        phone: str = "",
        website: str = "",
        city: str = "",
        state: str = "",
        niche: str = "",
        description: str = "",
    ) -> CRMSyncResult:
        result = CRMSyncResult(errors=[])
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            result.errors.append("email obrigatório para CRM")
            return result

        phone_ok = sanitize_phone(phone)
        name_for_person = (contact_name or company_name or "Contato").strip()
        first, last = split_name(name_for_person)
        account_name = (company_name or name_for_person)[:255]
        desc = description or f"Prospecção LG | nicho={niche}"

        # --- Account ---
        try:
            acc = await self.companies.get_or_create(
                account_name,
                website=website or "",
                phone=phone_ok,
                email=email,
                city=city or "",
                state=state or "",
                # NÃO enviar industry com valor de nicho (enum inválido no Espo)
                industry="",
                description=desc,
                account_type="Customer",
            )
            result.account_id = acc.get("id")
        except Exception as exc:
            msg = f"Account: {exc}"
            result.errors.append(msg)
            logger.warning("crm_account_failed", name=account_name, error=str(exc))
            # tenta sem phone/website
            try:
                acc = await self.client.create(
                    "Account",
                    {"name": account_name, "type": "Customer", "emailAddress": email},
                )
                result.account_id = acc.get("id")
                result.errors = [e for e in (result.errors or []) if e != msg]
            except Exception as exc2:
                result.errors.append(f"Account retry: {exc2}")

        # --- Contact (create pode devolver via 409 um "duplicado" por NOME —
        # ver _mismatched_email) ---
        try:
            existing = await self.contacts.find_by_email(email)
            if existing and existing.get("id"):
                result.contact_id = existing.get("id")
            else:
                ct = await self.contacts.create(
                    first,
                    last,
                    email=email,
                    phone=phone_ok,
                    account_id=result.account_id or "",
                    description=desc,
                )
                if _mismatched_email(ct, email):
                    logger.warning(
                        "crm_contact_name_collision",
                        intended_email=email,
                        reused_email=ct.get("emailAddress"),
                        reused_id=ct.get("id"),
                    )
                    ct = await self.contacts.create(
                        first,
                        _disambiguated_last_name(last, city, email),
                        email=email,
                        phone=phone_ok,
                        account_id=result.account_id or "",
                        description=desc,
                    )
                result.contact_id = ct.get("id")
            if result.account_id and result.contact_id:
                try:
                    await self.client.update(
                        "Contact",
                        result.contact_id,
                        {"accountId": result.account_id},
                    )
                except Exception:
                    pass
        except Exception as exc:
            result.errors.append(f"Contact: {exc}")
            logger.warning("crm_contact_failed", email=email, error=str(exc))
            try:
                ct = await self.client.create(
                    "Contact",
                    {
                        "firstName": first,
                        "lastName": last or ".",
                        "emailAddress": email,
                        **({"accountId": result.account_id} if result.account_id else {}),
                    },
                )
                if _mismatched_email(ct, email):
                    logger.warning(
                        "crm_contact_name_collision",
                        intended_email=email,
                        reused_email=ct.get("emailAddress"),
                        reused_id=ct.get("id"),
                    )
                    ct = await self.client.create(
                        "Contact",
                        {
                            "firstName": first,
                            "lastName": _disambiguated_last_name(last, city, email),
                            "emailAddress": email,
                            **({"accountId": result.account_id} if result.account_id else {}),
                        },
                    )
                result.contact_id = ct.get("id")
            except Exception as exc2:
                result.errors.append(f"Contact retry: {exc2}")

        # --- Lead ---
        try:
            existing_lead = await self.leads.find_by_email(email)
            if existing_lead and existing_lead.get("id"):
                result.lead_id = existing_lead.get("id")
            else:
                lead = await self.leads.create(
                    first,
                    last,
                    email=email,
                    phone=phone_ok,
                    account_name=account_name,
                    website=website or "",
                    source="Web Site",
                    status="New",
                    description=desc,
                    industry="",
                    address_city=city or "",
                    address_state=state or "",
                )
                if _mismatched_email(lead, email):
                    logger.warning(
                        "crm_lead_name_collision",
                        intended_email=email,
                        reused_email=lead.get("emailAddress"),
                        reused_id=lead.get("id"),
                    )
                    lead = await self.leads.create(
                        first,
                        _disambiguated_last_name(last, city, email),
                        email=email,
                        phone=phone_ok,
                        account_name=account_name,
                        website=website or "",
                        source="Web Site",
                        status="New",
                        description=desc,
                        industry="",
                        address_city=city or "",
                        address_state=state or "",
                    )
                result.lead_id = lead.get("id")
        except Exception as exc:
            result.errors.append(f"Lead: {exc}")
            logger.warning("crm_lead_failed", email=email, error=str(exc))
            try:
                lead = await self.client.create(
                    "Lead",
                    {
                        "firstName": first,
                        "lastName": last or ".",
                        "status": "New",
                        "source": "Web Site",
                        "emailAddress": email,
                        "accountName": account_name,
                    },
                )
                if _mismatched_email(lead, email):
                    logger.warning(
                        "crm_lead_name_collision",
                        intended_email=email,
                        reused_email=lead.get("emailAddress"),
                        reused_id=lead.get("id"),
                    )
                    lead = await self.client.create(
                        "Lead",
                        {
                            "firstName": first,
                            "lastName": _disambiguated_last_name(last, city, email),
                            "status": "New",
                            "source": "Web Site",
                            "emailAddress": email,
                            "accountName": account_name,
                        },
                    )
                result.lead_id = lead.get("id")
            except Exception as exc2:
                result.errors.append(f"Lead retry: {exc2}")

        logger.info(
            "crm_sync_done",
            email=email,
            account=result.account_id,
            contact=result.contact_id,
            lead=result.lead_id,
            errors=result.errors,
        )
        return result
