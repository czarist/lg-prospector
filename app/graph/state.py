"""Estado do LangGraph conforme spec."""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from typing_extensions import Annotated

import operator


class CampaignGraphState(TypedDict, total=False):
    campaign_id: str
    provider: str
    niche: str
    query: str
    city: str
    state: str
    max_results: int

    # Empresa / contato atuais (processamento item a item)
    company: dict[str, Any]
    contact: dict[str, Any]

    # Listas do pipeline
    companies: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    current_index: int

    # CRM
    crm_company_id: Optional[str]
    crm_contact_id: Optional[str]
    crm_lead_id: Optional[str]
    crm_opportunity_id: Optional[str]

    # Template / e-mail
    template_name: Optional[str]
    email_status: Optional[str]
    skip_email: bool  # True = prospecta sem disparar e-mail

    # Controle
    status: str
    paused: bool
    cancelled: bool
    error: Optional[str]
    logs: Annotated[list[str], operator.add]

    # Qualificação
    qualification_score: Optional[int]
    qualification_notes: Optional[str]
