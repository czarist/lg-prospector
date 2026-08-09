from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_campaign_service, get_stage_service
from app.api.schemas.campaigns import (
    CampaignCreate,
    CampaignResponse,
    CampaignStatusResponse,
    StageRunRequest,
    StageStatusResponse,
)
from app.api.schemas.common import MessageResponse
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService

router = APIRouter()


def _campaign_response(campaign) -> CampaignResponse:
    """Serializa campanha sem disparar lazy-load fora do await."""
    items = []
    try:
        raw_items = list(campaign.items) if campaign.items is not None else []
        for it in raw_items:
            items.append(
                {
                    "id": it.id,
                    "status": it.status,
                    "stage": getattr(it, "stage", None),
                    "company_domain": getattr(it, "company_domain", None),
                    "company_id": it.company_id,
                    "contact_id": it.contact_id,
                    "qualification_score": it.qualification_score,
                    "crm_lead_id": it.crm_lead_id,
                    "template_name": it.template_name,
                }
            )
    except Exception:
        items = []
    return CampaignResponse(
        id=campaign.id,
        name=campaign.name,
        niche=campaign.niche,
        provider=campaign.provider,
        query=campaign.query,
        city=campaign.city,
        state=campaign.state,
        status=campaign.status,
        current_stage=getattr(campaign, "current_stage", None) or "created",
        max_results=campaign.max_results,
        error_message=campaign.error_message,
        started_at=campaign.started_at,
        finished_at=campaign.finished_at,
        created_at=campaign.created_at,
        items=items,
    )


@router.post(
    "/campaign",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_campaign(
    body: CampaignCreate,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignResponse:
    """Cria campanha. Por padrão NÃO roda nada — use /stages/*."""
    cfg = dict(body.config or {})
    cfg.setdefault("skip_email", body.skip_email)
    try:
        campaign = await svc.create_campaign(
            name=body.name,
            niche=body.niche,
            query=body.query,
            city=body.city,
            state=body.state,
            max_results=body.max_results,
            config=cfg,
            run_async=body.run_async,
        )
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _campaign_response(campaign)


@router.get("/campaign/{campaign_id}/stages", response_model=StageStatusResponse)
async def get_stages(
    campaign_id: str,
    stages: StageService = Depends(get_stage_service),
) -> StageStatusResponse:
    try:
        data = await stages.stage_status(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StageStatusResponse(**data)


@router.post("/campaign/{campaign_id}/stages/{stage_name}")
async def run_campaign_stage(
    campaign_id: str,
    stage_name: str,
    body: StageRunRequest | None = None,
    stages: StageService = Depends(get_stage_service),
):
    """
    Executa uma etapa:
    - discover — busca empresas
    - enrich — e-mail do domínio do site
    - crm — Account/Contact/Lead no Espo
    - dispatch — envio SMTP (use dry_run=true para teste)
    """
    body = body or StageRunRequest()
    try:
        if stage_name == "dispatch":
            campaign = await stages.get_campaign(campaign_id)
            if not campaign:
                raise ValueError("Campanha não encontrada")
            result = await stages.stage_dispatch(
                campaign,
                dry_run=body.dry_run,
                max_send=body.max_send,
                delay_seconds=body.delay_seconds,
            )
            await stages.session.commit()
        else:
            result = await stages.run_stage(campaign_id, stage_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.get("/campaign/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignResponse:
    campaign = await svc.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campanha não encontrada")
    return _campaign_response(campaign)


@router.get("/campaign/{campaign_id}/status", response_model=CampaignStatusResponse)
async def get_campaign_status(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignStatusResponse:
    try:
        data = await svc.get_status(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CampaignStatusResponse(**data)


@router.post("/campaign/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignResponse:
    try:
        campaign = await svc.pause(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaign/{campaign_id}/resume", response_model=CampaignResponse)
async def resume_campaign(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignResponse:
    try:
        campaign = await svc.resume(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaign/{campaign_id}/cancel", response_model=CampaignResponse)
async def cancel_campaign(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> CampaignResponse:
    try:
        campaign = await svc.cancel(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaign/{campaign_id}/run", response_model=MessageResponse)
async def run_campaign_sync(
    campaign_id: str,
    svc: CampaignService = Depends(get_campaign_service),
) -> MessageResponse:
    """Executa a campanha de forma síncrona (útil para testes)."""
    try:
        final = await svc.run_campaign(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return MessageResponse(
        message="Campanha executada",
        detail=f"status={final.get('status')}",
    )
