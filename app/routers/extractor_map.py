"""Endpoint that accepts extractor-format invoices directly."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models.extractor import ExtractorInvoice
from app.models.response import MapResponse
from app.routers.map import MapOptions
from app.services.extractor_adapter import adapt
from app.services.mapper import MapperService

router = APIRouter(prefix="/api/v1", tags=["mapping"])


class ExtractorMapRequest(BaseModel):
    invoice: ExtractorInvoice
    options: MapOptions = Field(default_factory=MapOptions)


@router.post("/map/extractor", response_model=MapResponse)
async def map_extractor_invoice(
    body: ExtractorMapRequest,
    x_erp_system: Annotated[str, Header()],
    x_tenant_id: Annotated[str, Header()],
    settings: Settings = Depends(get_settings),
) -> MapResponse:
    """Accept an extractor-format invoice, adapt it, and run the mapping pipeline."""
    canonical = adapt(body.invoice)
    mapper = MapperService(settings)
    return mapper.map(
        invoice=canonical,
        erp_system=x_erp_system,
        tenant_id=x_tenant_id,
        options=body.options,
    )
