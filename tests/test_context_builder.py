"""Tests for the ContextBuilder service."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.context_builder import ContextBuilder


@pytest.fixture
def builder() -> ContextBuilder:
    settings = Settings(company_state_code="29")
    return ContextBuilder(settings)


class TestContextBuild:
    def test_same_state_gives_cgst_sgst(self, builder):
        ctx = builder.build(
            vendor_metadata={"state_code": "29", "category": "Raw Material"},
            vendor_erp_id="SUP-001",
            vendor_confidence=0.95,
        )
        assert ctx.tax_component == "CGST_SGST"
        assert ctx.vendor_known is True
        assert ctx.vendor_erp_id == "SUP-001"

    def test_different_state_gives_igst(self, builder):
        ctx = builder.build(
            vendor_metadata={"state_code": "33", "category": "Auto Parts"},
            vendor_erp_id="SUP-002",
            vendor_confidence=0.90,
        )
        assert ctx.tax_component == "IGST"

    def test_category_becomes_item_group_filter(self, builder):
        ctx = builder.build(
            vendor_metadata={"state_code": "29", "category": "Electrical"},
            vendor_erp_id="SUP-003",
            vendor_confidence=0.88,
        )
        assert ctx.item_group_filter == "Electrical"

    def test_no_state_code_gives_none_tax(self, builder):
        ctx = builder.build(
            vendor_metadata={},
            vendor_erp_id="SUP-004",
            vendor_confidence=0.85,
        )
        assert ctx.tax_component is None

    def test_high_vendor_confidence_lowers_floor(self, builder):
        ctx = builder.build(
            vendor_metadata={"state_code": "29"},
            vendor_erp_id="SUP-005",
            vendor_confidence=1.0,
        )
        assert ctx.confidence_floor < 0.50

    def test_low_vendor_confidence_keeps_floor_at_baseline(self, builder):
        ctx = builder.build(
            vendor_metadata={"state_code": "29"},
            vendor_erp_id="SUP-006",
            vendor_confidence=0.70,
        )
        assert ctx.confidence_floor == pytest.approx(0.50, abs=0.01)


class TestDeriveFromGstin:
    def test_same_state(self):
        result = ContextBuilder.derive_tax_component_from_gstin("29ABCDE1234F1Z5", "29")
        assert result == "CGST_SGST"

    def test_different_state(self):
        result = ContextBuilder.derive_tax_component_from_gstin("06AABCX9876P1ZQ", "29")
        assert result == "IGST"

    def test_empty_gstin_returns_none(self):
        assert ContextBuilder.derive_tax_component_from_gstin("", "29") is None

    def test_short_gstin_returns_none(self):
        assert ContextBuilder.derive_tax_component_from_gstin("2", "29") is None

    def test_non_digit_prefix_returns_none(self):
        assert ContextBuilder.derive_tax_component_from_gstin("XXABCDE1234", "29") is None
