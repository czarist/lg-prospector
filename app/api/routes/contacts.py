from fastapi import APIRouter, Depends, Query

from app.api.deps import get_campaign_service
from app.api.schemas.campaigns import ContactResponse
from app.api.schemas.common import PaginatedResponse
from app.services.campaign_service import CampaignService

router = APIRouter()


@router.get("/contacts", response_model=PaginatedResponse[ContactResponse])
async def list_contacts(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    svc: CampaignService = Depends(get_campaign_service),
) -> PaginatedResponse[ContactResponse]:
    items, total = await svc.list_contacts(offset=offset, limit=limit)
    return PaginatedResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[ContactResponse.model_validate(c) for c in items],
    )
