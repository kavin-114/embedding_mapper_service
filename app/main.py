"""FastAPI application entry point."""

from fastapi import FastAPI

from app.config import get_settings
from app.routers import feedback, health, map, sync


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(map.router)
    app.include_router(sync.router)
    app.include_router(feedback.router)
    return app


app = create_app()
