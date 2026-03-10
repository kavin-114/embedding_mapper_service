"""Collection sync endpoint — seeds / updates ERP master data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models.response import SyncResponse
from app.services.vector_service import VectorService
from app.services.embedding_service import EmbeddingService

router = APIRouter(prefix="/api/v1", tags=["sync"])


class SyncRequest(BaseModel):
    entity: str = Field(..., pattern="^(vendors|items|tax_codes|uoms)$")
    tenant_id: str
    erp_system: str
    records: list[dict[str, Any]]
    synced_at: datetime


@router.post("/sync", response_model=SyncResponse)
async def sync_collection(
    body: SyncRequest,
    settings: Settings = Depends(get_settings),
) -> SyncResponse:
    """Upsert ERP master data records into the appropriate ChromaDB collection."""
    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)
    count = vector_svc.upsert(
        entity=body.entity,
        tenant_id=body.tenant_id,
        erp_system=body.erp_system,
        records=body.records,
        synced_at=body.synced_at,
        embedding_fn=embedding_svc.encode,
    )
    return SyncResponse(
        entity=body.entity,
        tenant_id=body.tenant_id,
        erp_system=body.erp_system,
        records_upserted=count,
    )
