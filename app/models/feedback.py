"""Feedback models — captures human-approved mappings for learning."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ApprovedLineItem(BaseModel):
    """A single line-item mapping confirmed by human review."""

    description: str
    item_erp_id: str
    item_code: str | None = None
    hsn_code: str | None = None
    uom: str | None = None


class FeedbackRequest(BaseModel):
    """Payload sent after human review loop completes.

    The external system calls POST /api/v1/feedback with the approved
    vendor→item mappings.  These get stored in the vendor_context
    collection so future invoices from the same vendor resolve faster.
    """

    tenant_id: str
    erp_system: str
    invoice_number: str

    # Vendor details (human-verified)
    vendor_erp_id: str
    vendor_name: str
    vendor_tax_id: str | None = None

    # Approved line-item mappings
    line_items: list[ApprovedLineItem]

    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FeedbackResponse(BaseModel):
    """Response from POST /api/v1/feedback."""

    status: str
    records_upserted: int
    vendor_erp_id: str
    collection: str
