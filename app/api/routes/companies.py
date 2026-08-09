from fastapi import APIRouter, Depends, Query

from app.api.deps import get_campaign_service
from app.api.schemas.campaigns import CompanyResponse
from app.api.schemas.common import PaginatedResponse
from app.services.campaign_service import CampaignService

router = APIRouter()


@router.get("/companies", response_model=PaginatedResponse[CompanyResponse])
async def list_companies(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    segment: str | None = Query(None),
    svc: CampaignService = Depends(get_campaign_service),
) -> PaginatedResponse[CompanyResponse]:
    items, total = await svc.list_companies(offset=offset, limit=limit, segment=segment)
    return PaginatedResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[CompanyResponse.model_validate(c) for c in items],
    )
