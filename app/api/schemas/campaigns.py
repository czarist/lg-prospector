from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    niche: str = Field(
        ...,
        description="Nicho: advogado, agencia_marketing, empresa_ti, prestador_servico, grupo_midiatico, politico (alias: partido)",
    )
    query: str = Field(default="", max_length=512)
    city: str = Field(default="", max_length=128)
    state: str = Field(default="", max_length=64)
    max_results: int = Field(default=20, ge=1, le=200)
    config: Optional[dict[str, Any]] = None
    run_async: bool = Field(
        default=False,
        description="Se true, roda pipeline completo em background. Prefira etapas /stages/*",
    )
    skip_email: bool = Field(
        default=True,
        description="Se true, etapa dispatch não envia (só discover/enrich/crm)",
    )


class StageRunRequest(BaseModel):
    dry_run: bool = Field(default=False, description="Só na etapa dispatch: não envia de verdade")
    max_send: Optional[int] = Field(default=None, ge=1, le=500)
    delay_seconds: float = Field(default=1.5, ge=0, le=60)


class StageStatusResponse(BaseModel):
    campaign_id: str
    name: str
    niche: str
    status: str
    current_stage: str
    max_results: int
    items_by_stage: dict[str, int]
    stages: list[str]


class CampaignItemResponse(BaseModel):
    id: str
    status: str
    stage: Optional[str] = None
    company_domain: Optional[str] = None
    company_id: Optional[str] = None
    contact_id: Optional[str] = None
    qualification_score: Optional[int] = None
    crm_lead_id: Optional[str] = None
    template_name: Optional[str] = None

    model_config = {"from_attributes": True}


class CampaignResponse(BaseModel):
    id: str
    name: str
    niche: str
    provider: str
    query: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    status: str
    current_stage: Optional[str] = "created"
    max_results: int
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    items: list[CampaignItemResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class CampaignStatusResponse(BaseModel):
    id: str
    name: str
    status: str
    niche: str
    provider: str
    items_total: int
    items_by_status: dict[str, int]
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    graph_run: dict[str, Any]


class CompanyResponse(BaseModel):
    id: str
    name: str
    website: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    segment: Optional[str] = None
    source: Optional[str] = None
    crm_id: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ContactResponse(BaseModel):
    id: str
    company_id: Optional[str] = None
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    source: Optional[str] = None
    crm_id: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class DashboardResponse(BaseModel):
    campaigns_total: int
    campaigns_running: int
    campaigns_completed: int
    companies_total: int
    contacts_total: int
    campaigns_by_niche: dict[str, int]
    providers: list[dict[str, Any]]
