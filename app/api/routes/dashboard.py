from fastapi import APIRouter, Depends

from app.api.deps import get_campaign_service
from app.api.schemas.campaigns import DashboardResponse
from app.services.campaign_service import CampaignService

router = APIRouter()


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    svc: CampaignService = Depends(get_campaign_service),
) -> DashboardResponse:
    data = await svc.dashboard()
    return DashboardResponse(**data)
