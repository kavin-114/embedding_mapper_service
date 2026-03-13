"""Pydantic models matching the vLLM invoice extractor output format.

These models accept the extractor's JSON directly — confidence_score is an
integer 0-100 and field names follow ERPNext conventions.  The adapter
(extractor_adapter.py) converts these into CanonicalInvoice objects.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExtractorScoredField(BaseModel):
    """A value with integer confidence (0-100).

    The value can be a string, number, or None — the extractor outputs
    floats for qty/rate/amount and strings for names/codes.
    """

    value: Any = None
    confidence_score: int = Field(0, ge=0, le=100)


class ExtractorTaxIds(BaseModel):
    """Supplier tax identifiers extracted from the document."""

    gstin: ExtractorScoredField | None = None
    pan: ExtractorScoredField | None = None
    vat_id: ExtractorScoredField | None = None
    tax_id: ExtractorScoredField | None = None
    tax_id_type: ExtractorScoredField | None = None


class ExtractorAddress(BaseModel):
    """Address block (supplier or company).

    Each field is an ExtractorScoredField with value + confidence_score.
    """

    address_line1: ExtractorScoredField | None = None
    address_line2: ExtractorScoredField | None = None
    city: ExtractorScoredField | None = None
    state: ExtractorScoredField | None = None
    state_code: ExtractorScoredField | None = None
    country: ExtractorScoredField | None = None
    pincode: ExtractorScoredField | None = None
    phone: ExtractorScoredField | None = None
    email: ExtractorScoredField | None = None


class ExtractorLineItem(BaseModel):
    """Single line item from the extractor."""

    item_code: ExtractorScoredField | None = None
    item_name: ExtractorScoredField | None = None
    description: ExtractorScoredField | None = None
    qty: ExtractorScoredField | None = None
    uom: ExtractorScoredField | None = None
    rate: ExtractorScoredField | None = None
    amount: ExtractorScoredField | None = None
    discount_percentage: ExtractorScoredField | None = None
    hsn_sac: ExtractorScoredField | None = None
    batch_no: ExtractorScoredField | None = None
    serial_no: ExtractorScoredField | None = None


class ExtractorTaxLine(BaseModel):
    """A single tax/charge line from the extractor."""

    charge_type: ExtractorScoredField | None = None
    account_head: ExtractorScoredField | None = None
    description: ExtractorScoredField | None = None
    rate: ExtractorScoredField | None = None
    tax_amount: ExtractorScoredField | None = None


class ExtractorInvoice(BaseModel):
    """Top-level model matching the vLLM invoice extractor output."""

    model_config = {"extra": "ignore"}

    company: ExtractorScoredField | None = None
    supplier: ExtractorScoredField | None = None
    supplier_name: ExtractorScoredField | None = None
    supplier_tax_ids: ExtractorTaxIds | None = None
    supplier_address: ExtractorAddress | None = None
    billing_address: ExtractorAddress | None = None
    shipping_address: ExtractorAddress | None = None
    bill_no: ExtractorScoredField | None = None
    bill_date: ExtractorScoredField | None = None
    posting_date: ExtractorScoredField | None = None
    currency: ExtractorScoredField | None = None
    grand_total: ExtractorScoredField | None = None
    total: ExtractorScoredField | None = None
    discount_amount: ExtractorScoredField | None = None
    items: list[ExtractorLineItem] = Field(default_factory=list)
    taxes: list[ExtractorTaxLine] = Field(default_factory=list)
