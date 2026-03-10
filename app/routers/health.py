"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.services.vector_service import VectorService

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Basic liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(settings: Settings = Depends(get_settings)):
    """Readiness probe — verifies ChromaDB is reachable."""
    try:
        vs = VectorService(settings)
        vs.client.heartbeat()
        return {"status": "ready", "chroma": "connected"}
    except Exception as exc:
        return {"status": "not_ready", "chroma": str(exc)}
