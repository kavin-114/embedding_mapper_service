"""Canonical invoice models.

These represent the standardised invoice structure produced by the vLLM
document parser.  Every foreign-key field is wrapped in a ScoredField
whose *confidence* is assigned upstream by vLLM — this service never
computes confidence for incoming data, only for its own resolution results.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ScoredField(BaseModel):
    """A value paired with vLLM extraction confidence."""

    value: Any
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="vLLM extraction confidence for this field",
    )


class CanonicalLineItem(BaseModel):
    """Single line item on a canonical invoice.

    FK fields (description, uom, tax_rate, hsn_code, item_code) are
    ScoredFields because they need vector resolution.  Plain numeric
    fields (quantity, unit_price) are bare values.
    """

    description: ScoredField
    quantity: float
    unit_price: float
    uom: ScoredField
    tax_rate: ScoredField
    hsn_code: ScoredField | None = None
    item_code: ScoredField | None = None


class CanonicalInvoice(BaseModel):
    """Top-level canonical invoice produced by vLLM document parser."""

    invoice_number: str
    invoice_date: date
    vendor_name: ScoredField
    vendor_tax_id: ScoredField | None = None
    vendor_tax_id_type: str | None = None
    currency: str = "INR"
    total_amount: float
    line_items: list[CanonicalLineItem]
    company_name: str | None = None
    supplier_country: str | None = None
    supplier_region_code: str | None = None
