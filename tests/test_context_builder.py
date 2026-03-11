"""Tests for the ContextBuilder service."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.context_builder import ContextBuilder


@pytest.fixture
def builder() -> ContextBuilder:
    settings = Settings(company_country="IN", company_region_code="29")
    return ContextBuilder(settings)


class TestContextBuild:
    def test_same_region_gives_intra_region(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29", "category": "Raw Material"},
            vendor_erp_id="SUP-001",
            vendor_confidence=0.95,
        )
        assert ctx.tax_scope == "INTRA_REGION"
        assert ctx.vendor_known is True
        assert ctx.vendor_erp_id == "SUP-001"

    def test_different_region_gives_inter_region(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "33", "category": "Auto Parts"},
            vendor_erp_id="SUP-002",
            vendor_confidence=0.90,
        )
        assert ctx.tax_scope == "INTER_REGION"

    def test_different_country_gives_import(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "US", "region_code": "CA", "category": "Electronics"},
            vendor_erp_id="SUP-003",
            vendor_confidence=0.90,
        )
        assert ctx.tax_scope == "IMPORT"

    def test_category_becomes_item_group_filter(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29", "category": "Electrical"},
            vendor_erp_id="SUP-003",
            vendor_confidence=0.88,
        )
        assert ctx.item_group_filter == "Electrical"

    def test_no_country_gives_none_tax_scope(self, builder):
        ctx = builder.build(
            vendor_metadata={},
            vendor_erp_id="SUP-004",
            vendor_confidence=0.85,
        )
        assert ctx.tax_scope is None

    def test_high_vendor_confidence_lowers_floor(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29"},
            vendor_erp_id="SUP-005",
            vendor_confidence=1.0,
        )
        assert ctx.confidence_floor < 0.50

    def test_low_vendor_confidence_keeps_floor_at_baseline(self, builder):
        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29"},
            vendor_erp_id="SUP-006",
            vendor_confidence=0.70,
        )
        assert ctx.confidence_floor == pytest.approx(0.50, abs=0.01)


class TestDeriveTaxScope:
    def test_same_country_same_region(self):
        result = ContextBuilder.derive_tax_scope("IN", "29", "IN", "29")
        assert result == "INTRA_REGION"

    def test_same_country_different_region(self):
        result = ContextBuilder.derive_tax_scope("IN", "33", "IN", "29")
        assert result == "INTER_REGION"

    def test_different_country(self):
        result = ContextBuilder.derive_tax_scope("US", "CA", "IN", "29")
        assert result == "IMPORT"

    def test_empty_vendor_country_returns_none(self):
        assert ContextBuilder.derive_tax_scope("", "29", "IN", "29") is None

    def test_empty_company_country_returns_none(self):
        assert ContextBuilder.derive_tax_scope("IN", "29", "", "29") is None

    def test_same_country_no_regions_returns_none(self):
        assert ContextBuilder.derive_tax_scope("IN", "", "IN", "") is None
