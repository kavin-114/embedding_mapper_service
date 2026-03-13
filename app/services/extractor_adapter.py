"""Adapter: convert ExtractorInvoice → CanonicalInvoice.

Handles confidence scaling (int 0-100 → float 0.0-1.0), field renaming,
tax ID priority selection, and document-level tax distribution to line items.
"""

from __future__ import annotations

from datetime import date

from app.models.canonical import CanonicalInvoice, CanonicalLineItem, ScoredField
from app.models.extractor import (
    ExtractorInvoice,
    ExtractorLineItem,
    ExtractorScoredField,
    ExtractorTaxIds,
)


def adapt(extractor: ExtractorInvoice) -> CanonicalInvoice:
    """Convert an ExtractorInvoice into a CanonicalInvoice."""
    # Use supplier_name if available, else fall back to supplier
    vendor_field = extractor.supplier_name if _has_value(extractor.supplier_name) else extractor.supplier
    vendor_name = _to_scored(vendor_field)
    vendor_tax_id, vendor_tax_id_type = _pick_tax_id(extractor.supplier_tax_ids)

    invoice_number = _str_value(extractor.bill_no, "UNKNOWN")
    date_field = extractor.bill_date if _has_value(extractor.bill_date) else extractor.posting_date
    invoice_date = _parse_date(date_field)
    currency = _str_value(extractor.currency, "INR")
    total_field = extractor.grand_total if _has_value(extractor.grand_total) else extractor.total
    total_amount = _float_value(
        total_field, 0.0,
    )

    # Derive per-line tax rate from document-level taxes
    per_line_tax_rate = _compute_per_line_tax_rate(extractor)

    line_items = [
        _adapt_line_item(li, per_line_tax_rate)
        for li in extractor.items
    ]

    # Extra fields from extractor
    company_name: str | None = None
    if extractor.company and extractor.company.value:
        company_name = str(extractor.company.value)

    supplier_country: str | None = None
    supplier_region_code: str | None = None
    if extractor.supplier_address:
        supplier_country = _addr_str(extractor.supplier_address.country)
        supplier_region_code = _addr_str(extractor.supplier_address.state_code)

    return CanonicalInvoice(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        vendor_name=vendor_name,
        vendor_tax_id=vendor_tax_id,
        vendor_tax_id_type=vendor_tax_id_type,
        currency=currency,
        total_amount=total_amount,
        line_items=line_items,
        company_name=company_name,
        supplier_country=supplier_country,
        supplier_region_code=supplier_region_code,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_value(field: ExtractorScoredField | None) -> bool:
    """Check if a scored field has a non-empty value."""
    if field is None:
        return False
    v = field.value
    if v is None:
        return False
    if isinstance(v, str) and v == "":
        return False
    return True


def _to_scored(
    field: ExtractorScoredField | None,
    default_value: str = "",
) -> ScoredField:
    """Convert an ExtractorScoredField to a ScoredField (confidence / 100)."""
    if not _has_value(field):
        return ScoredField(value=default_value, confidence=0.0)
    return ScoredField(
        value=field.value,
        confidence=field.confidence_score / 100.0,
    )


def _to_scored_optional(
    field: ExtractorScoredField | None,
) -> ScoredField | None:
    """Convert to ScoredField, returning None if value is empty."""
    if not _has_value(field):
        return None
    return ScoredField(
        value=field.value,
        confidence=field.confidence_score / 100.0,
    )


def _str_value(field: ExtractorScoredField | None, default: str) -> str:
    if not _has_value(field):
        return default
    return str(field.value)


def _float_value(field: ExtractorScoredField | None, default: float) -> float:
    if not _has_value(field):
        return default
    try:
        return float(field.value)
    except (ValueError, TypeError):
        return default


def _addr_str(field: ExtractorScoredField | None) -> str | None:
    """Extract a plain string from an address scored field."""
    if not _has_value(field):
        return None
    return str(field.value)


def _parse_date(field: ExtractorScoredField | None) -> date:
    """Parse a date string from the extractor. Falls back to today."""
    if not _has_value(field):
        return date.today()
    try:
        return date.fromisoformat(str(field.value))
    except ValueError:
        return date.today()


def _pick_tax_id(
    tax_ids: ExtractorTaxIds | None,
) -> tuple[ScoredField | None, str | None]:
    """Select the best tax ID from the extractor's tax ID block.

    Priority: GSTIN > VAT > generic tax_id > PAN.
    Returns (scored_field, tax_id_type_string).
    """
    if tax_ids is None:
        return None, None

    # Check in priority order
    candidates: list[tuple[ExtractorScoredField | None, str]] = [
        (tax_ids.gstin, "GSTIN"),
        (tax_ids.vat_id, "VAT"),
        (tax_ids.tax_id, _infer_tax_id_type(tax_ids)),
        (tax_ids.pan, "PAN"),
    ]

    for field, id_type in candidates:
        if _has_value(field) and field.confidence_score > 0:
            scored = ScoredField(
                value=str(field.value),
                confidence=field.confidence_score / 100.0,
            )
            return scored, id_type

    return None, None


def _infer_tax_id_type(tax_ids: ExtractorTaxIds) -> str:
    """Get the tax_id_type string, defaulting to 'TIN'."""
    if _has_value(tax_ids.tax_id_type):
        return str(tax_ids.tax_id_type.value)
    return "TIN"


def _compute_per_line_tax_rate(extractor: ExtractorInvoice) -> ScoredField:
    """Derive a per-line tax rate from document-level taxes.

    - No taxes → rate 0, confidence 0.0
    - Single tax line → use that rate directly, confidence 0.80
    - Multiple tax lines → sum rates, confidence 0.60
    """
    if not extractor.taxes:
        return ScoredField(value=0.0, confidence=0.0)

    total_rate = sum(_float_value(t.rate, 0.0) for t in extractor.taxes)

    if len(extractor.taxes) == 1:
        return ScoredField(value=total_rate, confidence=0.80)

    return ScoredField(value=total_rate, confidence=0.60)


def _adapt_line_item(
    li: ExtractorLineItem,
    fallback_tax_rate: ScoredField,
) -> CanonicalLineItem:
    """Convert a single extractor line item to canonical format."""
    # description: prefer item_name, fall back to description, then item_code
    description = _to_scored(li.item_name)
    if description.confidence == 0.0:
        description = _to_scored(li.description)
    if description.confidence == 0.0:
        description = _to_scored(li.item_code)

    quantity = _float_value(li.qty, 1.0)
    unit_price = _float_value(li.rate, 0.0)
    uom = _to_scored(li.uom, "")
    hsn_code = _to_scored_optional(li.hsn_sac)
    item_code = _to_scored_optional(li.item_code)

    return CanonicalLineItem(
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        uom=uom,
        tax_rate=fallback_tax_rate,
        hsn_code=hsn_code,
        item_code=item_code,
    )
