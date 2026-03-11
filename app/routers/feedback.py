"""Feedback endpoint — receives human-approved mappings to learn from."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.models.feedback import FeedbackRequest, FeedbackResponse
from app.services.embedding_service import EmbeddingService
from app.services.vector_service import VectorService

router = APIRouter(prefix="/api/v1", tags=["feedback"])


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    body: FeedbackRequest,
    settings: Settings = Depends(get_settings),
) -> FeedbackResponse:
    """Store human-approved vendor→item mappings for future resolution.

    Called after the human review loop completes. Updates the
    vendor_context collection so future invoices from the same
    vendor resolve with higher confidence.
    """
    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)

    items = [
        {
            "item_erp_id": li.item_erp_id,
            "item_code": li.item_code,
            "hsn_code": li.hsn_code,
            "uom": li.uom,
            "description": li.description,
        }
        for li in body.line_items
    ]

    count = vector_svc.upsert_vendor_context(
        tenant_id=body.tenant_id,
        erp_system=body.erp_system,
        vendor_erp_id=body.vendor_erp_id,
        vendor_name=body.vendor_name,
        vendor_tax_id=body.vendor_tax_id,
        items=items,
        embedding_fn=embedding_svc.encode,
    )

    collection_name = VectorService.collection_name(
        "vendor_context", body.tenant_id, body.erp_system
    )

    return FeedbackResponse(
        status="ok",
        records_upserted=count,
        vendor_erp_id=body.vendor_erp_id,
        collection=collection_name,
    )
