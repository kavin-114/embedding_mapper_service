"""FastAPI application entry point."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.logging_config import setup_logging
from app.middleware import RequestContextMiddleware
from app.routers import extractor_map, feedback, health, map, pull_sync, review, sync

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(log_format=settings.log_format, log_level=settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(map.router)
    app.include_router(sync.router)
    app.include_router(pull_sync.router)
    app.include_router(feedback.router)
    app.include_router(extractor_map.router)
    app.include_router(review.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
