"""Tests for the extractor → canonical adapter."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.models.extractor import (
    ExtractorInvoice,
    ExtractorLineItem,
    ExtractorScoredField,
    ExtractorTaxIds,
    ExtractorTaxLine,
    ExtractorAddress,
)
from app.services.extractor_adapter import (
    adapt,
    _compute_per_line_tax_rate,
    _has_value,
    _pick_tax_id,
    _to_scored,
    _to_scored_optional,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ── Confidence scaling ──────────────────────────────────────────────


class TestConfidenceScaling:
    def test_normal_confidence(self):
        sf = _to_scored(ExtractorScoredField(value="x", confidence_score=95))
        assert sf.confidence == 0.95

    def test_zero_confidence(self):
        sf = _to_scored(ExtractorScoredField(value="x", confidence_score=0))
        assert sf.confidence == 0.0

    def test_full_confidence(self):
        sf = _to_scored(ExtractorScoredField(value="x", confidence_score=100))
        assert sf.confidence == 1.0

    def test_none_field_returns_default(self):
        sf = _to_scored(None, "fallback")
        assert sf.value == "fallback"
        assert sf.confidence == 0.0

    def test_empty_value_returns_default(self):
        sf = _to_scored(ExtractorScoredField(value="", confidence_score=80))
        assert sf.value == ""
        assert sf.confidence == 0.0

    def test_optional_none_field(self):
        assert _to_scored_optional(None) is None

    def test_optional_empty_value(self):
        assert _to_scored_optional(
            ExtractorScoredField(value="", confidence_score=50)
        ) is None

    def test_optional_with_value(self):
        sf = _to_scored_optional(
            ExtractorScoredField(value="ABC", confidence_score=70)
        )
        assert sf is not None
        assert sf.value == "ABC"
        assert sf.confidence == 0.70


# ── Tax ID priority ─────────────────────────────────────────────────


class TestTaxIdPriority:
    def test_gstin_preferred_over_others(self):
        tax_ids = ExtractorTaxIds(
            gstin=ExtractorScoredField(value="29AABCK1234H1Z5", confidence_score=90),
            pan=ExtractorScoredField(value="AABCK1234H", confidence_score=85),
        )
        scored, id_type = _pick_tax_id(tax_ids)
        assert scored is not None
        assert scored.value == "29AABCK1234H1Z5"
        assert id_type == "GSTIN"

    def test_vat_when_no_gstin(self):
        tax_ids = ExtractorTaxIds(
            vat_id=ExtractorScoredField(value="GB123456789", confidence_score=88),
        )
        scored, id_type = _pick_tax_id(tax_ids)
        assert scored is not None
        assert scored.value == "GB123456789"
        assert id_type == "VAT"

    def test_generic_tax_id_fallback(self):
        tax_ids = ExtractorTaxIds(
            tax_id=ExtractorScoredField(value="12-3456789", confidence_score=75),
            tax_id_type=ExtractorScoredField(value="EIN", confidence_score=80),
        )
        scored, id_type = _pick_tax_id(tax_ids)
        assert scored is not None
        assert scored.value == "12-3456789"
        assert id_type == "EIN"

    def test_none_tax_ids(self):
        scored, id_type = _pick_tax_id(None)
        assert scored is None
        assert id_type is None

    def test_all_empty_tax_ids(self):
        tax_ids = ExtractorTaxIds()
        scored, id_type = _pick_tax_id(tax_ids)
        assert scored is None
        assert id_type is None


# ── Tax distribution ─────────────────────────────────────────────────


class TestTaxDistribution:
    def test_no_taxes(self):
        inv = ExtractorInvoice(items=[], taxes=[])
        rate = _compute_per_line_tax_rate(inv)
        assert rate.value == 0.0
        assert rate.confidence == 0.0

    def test_single_tax(self):
        inv = ExtractorInvoice(
            items=[],
            taxes=[ExtractorTaxLine(
                rate=ExtractorScoredField(value=18.0, confidence_score=85),
                tax_amount=ExtractorScoredField(value=1800.0, confidence_score=80),
            )],
        )
        rate = _compute_per_line_tax_rate(inv)
        assert rate.value == 18.0
        assert rate.confidence == 0.80

    def test_multiple_taxes_summed(self):
        inv = ExtractorInvoice(
            items=[],
            taxes=[
                ExtractorTaxLine(
                    rate=ExtractorScoredField(value=9.0, confidence_score=85),
                    tax_amount=ExtractorScoredField(value=900.0, confidence_score=80),
                ),
                ExtractorTaxLine(
                    rate=ExtractorScoredField(value=9.0, confidence_score=85),
                    tax_amount=ExtractorScoredField(value=900.0, confidence_score=80),
                ),
            ],
        )
        rate = _compute_per_line_tax_rate(inv)
        assert rate.value == 18.0
        assert rate.confidence == 0.60


# ── Field mapping ────────────────────────────────────────────────────


class TestFieldMapping:
    def test_basic_field_mapping(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="Acme Corp", confidence_score=95),
            bill_no=ExtractorScoredField(value="INV-001", confidence_score=98),
            bill_date=ExtractorScoredField(value="2026-03-01", confidence_score=90),
            grand_total=ExtractorScoredField(value="1000.00", confidence_score=88),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Widget", confidence_score=90),
                    qty=ExtractorScoredField(value="5", confidence_score=95),
                    rate=ExtractorScoredField(value="200.00", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=92),
                ),
            ],
        )
        canonical = adapt(ext)

        assert canonical.invoice_number == "INV-001"
        assert canonical.invoice_date == date(2026, 3, 1)
        assert canonical.vendor_name.value == "Acme Corp"
        assert canonical.vendor_name.confidence == 0.95
        assert canonical.total_amount == 1000.0
        assert len(canonical.line_items) == 1
        assert canonical.line_items[0].description.value == "Widget"
        assert canonical.line_items[0].quantity == 5.0
        assert canonical.line_items[0].unit_price == 200.0
        assert canonical.line_items[0].uom.value == "Nos"

    def test_empty_supplier_name_falls_back_to_supplier(self):
        """When supplier_name is empty, adapter should use supplier field."""
        ext = ExtractorInvoice(
            supplier=ExtractorScoredField(value="Acme Corp", confidence_score=95),
            supplier_name=ExtractorScoredField(value="", confidence_score=0),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.vendor_name.value == "Acme Corp"
        assert canonical.vendor_name.confidence == 0.95

    def test_missing_bill_no_defaults(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="X", confidence_score=80),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.invoice_number == "UNKNOWN"

    def test_missing_uom_defaults_to_empty(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="X", confidence_score=80),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    # no uom provided
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.line_items[0].uom.value == ""
        assert canonical.line_items[0].uom.confidence == 0.0

    def test_invalid_date_falls_back_to_today(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="X", confidence_score=80),
            bill_date=ExtractorScoredField(value="not-a-date", confidence_score=50),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.invoice_date == date.today()


# ── Extra data passthrough ───────────────────────────────────────────


class TestExtraDataPassthrough:
    def test_company_name_passed(self):
        ext = ExtractorInvoice(
            company=ExtractorScoredField(value="My Company", confidence_score=95),
            supplier_name=ExtractorScoredField(value="V", confidence_score=80),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.company_name == "My Company"

    def test_supplier_geography_passed(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="V", confidence_score=80),
            supplier_address=ExtractorAddress(
                country=ExtractorScoredField(value="IN", confidence_score=90),
                state_code=ExtractorScoredField(value="29", confidence_score=85),
            ),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.supplier_country == "IN"
        assert canonical.supplier_region_code == "29"

    def test_no_extra_data_defaults_to_none(self):
        ext = ExtractorInvoice(
            supplier_name=ExtractorScoredField(value="V", confidence_score=80),
            grand_total=ExtractorScoredField(value="100", confidence_score=80),
            items=[
                ExtractorLineItem(
                    item_name=ExtractorScoredField(value="Y", confidence_score=80),
                    qty=ExtractorScoredField(value="1", confidence_score=95),
                    rate=ExtractorScoredField(value="100", confidence_score=90),
                    uom=ExtractorScoredField(value="Nos", confidence_score=90),
                ),
            ],
        )
        canonical = adapt(ext)
        assert canonical.company_name is None
        assert canonical.supplier_country is None
        assert canonical.supplier_region_code is None


# ── Fixture-based integration test ───────────────────────────────────


class TestFixtureIntegration:
    def test_sample_extractor_invoice(self):
        raw = json.loads((FIXTURES / "sample_extractor_invoice.json").read_text())
        ext = ExtractorInvoice(**raw)
        canonical = adapt(ext)

        assert canonical.invoice_number == "INV-2026-0042"
        assert canonical.invoice_date == date(2026, 3, 1)
        assert canonical.vendor_name.value == "Kailash Electricals"
        assert canonical.vendor_name.confidence == 0.95
        assert canonical.vendor_tax_id is not None
        assert canonical.vendor_tax_id.value == "29AABCK1234H1Z5"
        assert canonical.vendor_tax_id_type == "GSTIN"
        assert canonical.currency == "INR"
        assert canonical.total_amount == 11800.0
        assert canonical.company_name == "Ambrosia Supplies Pvt Ltd"
        assert canonical.supplier_country == "IN"
        assert canonical.supplier_region_code == "29"

        assert len(canonical.line_items) == 2

        li0 = canonical.line_items[0]
        assert li0.description.value == "Copper Wire 2.5mm"
        assert li0.quantity == 100.0
        assert li0.unit_price == 50.0
        assert li0.uom.value == "Meter"
        assert li0.hsn_code is not None
        assert li0.hsn_code.value == "7408"
        assert li0.item_code is not None
        assert li0.item_code.value == "WIRE-CU-2.5"
        # Two taxes summed → 18%, confidence 0.60
        assert li0.tax_rate.value == 18.0
        assert li0.tax_rate.confidence == 0.60

        li1 = canonical.line_items[1]
        assert li1.description.value == "MCB 32A Single Pole"
        assert li1.quantity == 10.0
        assert li1.unit_price == 500.0
