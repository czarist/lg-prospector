from fastapi import APIRouter

from app.api.routes.campaigns import router as campaigns_router
from app.api.routes.companies import router as companies_router
from app.api.routes.contacts import router as contacts_router
from app.api.routes.dashboard import router as dashboard_router

api_router = APIRouter()
api_router.include_router(campaigns_router, tags=["campaigns"])
api_router.include_router(companies_router, tags=["companies"])
api_router.include_router(contacts_router, tags=["contacts"])
api_router.include_router(dashboard_router, tags=["dashboard"])
