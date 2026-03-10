"""Invoice mapping endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models.canonical import CanonicalInvoice
from app.models.response import MapResponse
from app.services.mapper import MapperService

router = APIRouter(prefix="/api/v1", tags=["mapping"])


class MapOptions(BaseModel):
    confidence_threshold: float = Field(0.88, ge=0.0, le=1.0)
    return_candidates: bool = True
    dry_run: bool = False


class MapRequest(BaseModel):
    invoice: CanonicalInvoice
    options: MapOptions = Field(default_factory=MapOptions)


@router.post("/map", response_model=MapResponse)
async def map_invoice(
    body: MapRequest,
    x_erp_system: Annotated[str, Header()],
    x_tenant_id: Annotated[str, Header()],
    settings: Settings = Depends(get_settings),
) -> MapResponse:
    """Resolve FK fields in a canonical invoice and transform to ERP payload."""
    mapper = MapperService(settings)
    return mapper.map(
        invoice=body.invoice,
        erp_system=x_erp_system,
        tenant_id=x_tenant_id,
        options=body.options,
    )
