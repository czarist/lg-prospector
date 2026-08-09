"""Entry point FastAPI — LG Prospector."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import api_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.infrastructure.database.session import dispose_db, init_db
from app.infrastructure.redis.client import close_redis, get_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info("starting", app=settings.app_name, env=settings.app_env, version=__version__)

    await init_db()
    logger.info("database_ready")

    # Seed providers
    from app.infrastructure.database.session import async_session_factory
    from app.services.campaign_service import CampaignService

    factory = async_session_factory()
    async with factory() as session:
        svc = CampaignService(session)
        n = await svc.seed_providers()
        await session.commit()
        if n:
            logger.info("providers_seeded", count=n)

    try:
        r = await get_redis()
        await r.ping()
        logger.info("redis_ready")
    except Exception as exc:
        logger.warning("redis_unavailable", error=str(exc))

    yield

    await close_redis()
    await dispose_db()
    logger.info("shutdown_complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LG Prospector",
        description="Plataforma de prospecção B2B com LangGraph e EspoCRM",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "app": settings.app_name}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.is_development,
    )


if __name__ == "__main__":
    run()
