"""Dependency injection FastAPI."""

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import async_session_factory
from app.services.campaign_service import CampaignService
from app.services.stage_service import StageService


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_campaign_service(
    session: AsyncSession = Depends(get_db),
) -> CampaignService:
    return CampaignService(session)


async def get_stage_service(
    session: AsyncSession = Depends(get_db),
) -> StageService:
    return StageService(session)
