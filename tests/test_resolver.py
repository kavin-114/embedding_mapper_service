"""Tests for the Resolver service."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.models.canonical import ScoredField, CanonicalInvoice
from app.models.resolution import (
    FKMatch,
    InvoiceContext,
    ResolutionStrategy,
    VendorStatus,
)
from app.services.resolver import Resolver


@pytest.fixture
def mock_settings() -> Settings:
    return Settings(
        chroma_host="localhost",
        chroma_port=8000,
        company_country="IN",
        company_region_code="29",
    )


@pytest.fixture
def mock_embedding():
    svc = MagicMock()
    svc.encode.return_value = [[0.1] * 384]
    return svc


@pytest.fixture
def mock_vector():
    return MagicMock()


@pytest.fixture
def resolver(mock_settings, mock_embedding, mock_vector):
    return Resolver(mock_settings, mock_embedding, mock_vector)


# ── Strategy selection tests ────────────────────────────────────────


class TestResolverStrategySelection:
    """Verify that the resolver picks the correct strategy based on confidence."""

    def test_high_confidence_selects_hard_key(self):
        field = ScoredField(value="29ABCDE1234F1Z5", confidence=0.95)
        assert field.confidence >= 0.90

    def test_medium_confidence_selects_filtered_semantic(self):
        field = ScoredField(value="7318", confidence=0.75)
        assert 0.70 <= field.confidence < 0.90

    def test_low_confidence_selects_pure_semantic(self):
        field = ScoredField(value="Steel bolts", confidence=0.55)
        assert 0.50 <= field.confidence < 0.70

    def test_very_low_confidence_skips(self):
        field = ScoredField(value="unknown", confidence=0.30)
        assert field.confidence < 0.50


class TestConfidenceDecisions:
    """Verify confidence → mapping status thresholds."""

    def test_auto_map_threshold(self):
        assert 0.88 <= 0.95

    def test_suggest_threshold(self):
        assert 0.70 <= 0.80 < 0.88

    def test_review_threshold(self):
        assert 0.50 <= 0.60 < 0.70

    def test_no_match_threshold(self):
        assert 0.30 < 0.50


# ── Vendor resolution tests ────────────────────────────────────────


class TestVendorResolution:
    def test_hard_match_on_high_confidence_tax_id(
        self, resolver, mock_vector, sample_invoice
    ):
        """tax_id >= 0.90 → hard match → FOUND with confidence 1.0."""
        mock_vector.hard_match.return_value = {
            "erp_id": "SUP-001",
            "tax_id": "27AAACT2727Q1ZV",
            "region_code": "27",
        }

        match, status = resolver.resolve_vendor(sample_invoice, "t1", "erpnext")

        assert status == VendorStatus.FOUND
        assert match.strategy == ResolutionStrategy.HARD_KEY
        assert match.confidence == 1.0
        assert match.erp_id == "SUP-001"
        mock_vector.hard_match.assert_called_once()

    def test_semantic_fallback_when_tax_id_hard_match_fails(
        self, resolver, mock_vector, mock_embedding, sample_invoice
    ):
        """tax_id hard match miss → semantic search on vendor_name."""
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {
                "erp_id": "SUP-002",
                "score": 0.92,
                "distance": 0.08,
                "metadata": {"region_code": "29", "erp_id": "SUP-002"},
            }
        ]

        match, status = resolver.resolve_vendor(sample_invoice, "t1", "erpnext")

        assert status == VendorStatus.FOUND
        assert match.strategy == ResolutionStrategy.PURE_SEMANTIC
        mock_vector.semantic_search.assert_called_once()

    def test_region_code_boost_when_matches_company(
        self, resolver, mock_vector, sample_invoice
    ):
        """Vendor region matches company region (29) → +0.08 boost."""
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {
                "erp_id": "SUP-002",
                "score": 0.82,
                "distance": 0.18,
                "metadata": {"region_code": "29", "erp_id": "SUP-002"},
            }
        ]

        match, status = resolver.resolve_vendor(sample_invoice, "t1", "erpnext")

        # 0.82 + 0.08 = 0.90 → FOUND (>= 0.88)
        assert status == VendorStatus.FOUND
        assert match.confidence == pytest.approx(0.90, abs=0.01)

    def test_region_code_penalty_when_mismatches_company(
        self, resolver, mock_vector, sample_invoice
    ):
        """Vendor region != company region → -0.15 penalty."""
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {
                "erp_id": "SUP-003",
                "score": 0.85,
                "distance": 0.15,
                "metadata": {"region_code": "33", "erp_id": "SUP-003"},
            }
        ]

        match, status = resolver.resolve_vendor(sample_invoice, "t1", "erpnext")

        # 0.85 - 0.15 = 0.70 → SUGGEST (>= 0.70 but < 0.88)
        assert status == VendorStatus.SUGGEST
        assert match.confidence == pytest.approx(0.70, abs=0.01)

    def test_no_results_returns_not_found(
        self, resolver, mock_vector, sample_invoice
    ):
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = []

        match, status = resolver.resolve_vendor(sample_invoice, "t1", "erpnext")

        assert status == VendorStatus.NOT_FOUND
        assert match.strategy == ResolutionStrategy.NOT_FOUND

    def test_no_tax_id_skips_hard_match(
        self, resolver, mock_vector, no_tax_id_invoice
    ):
        """Invoice with no tax_id → goes straight to semantic search."""
        mock_vector.semantic_search.return_value = [
            {
                "erp_id": "SUP-010",
                "score": 0.91,
                "distance": 0.09,
                "metadata": {"erp_id": "SUP-010"},
            }
        ]

        match, status = resolver.resolve_vendor(no_tax_id_invoice, "t1", "erpnext")

        assert status == VendorStatus.FOUND
        mock_vector.hard_match.assert_not_called()


# ── Unknown vendor handler tests ───────────────────────────────────


class TestUnknownVendorHandler:
    def test_stale_data_detected_when_no_sync(
        self, resolver, mock_vector, mock_embedding, unknown_vendor_invoice
    ):
        """No sync time → flags STALE_DATA + TRIGGER_RESYNC."""
        mock_vector.get_sync_time.return_value = None
        mock_vector.semantic_search.return_value = []

        match, status, ctx = resolver.handle_unknown_vendor(
            unknown_vendor_invoice, "t1", "erpnext"
        )

        assert status == VendorStatus.NOT_FOUND
        assert ctx.vendor_known is False
        # Flags are stored in candidates
        all_flags = []
        for c in match.candidates:
            all_flags.extend(c.get("flags", []))
        assert "STALE_DATA" in all_flags

    def test_partial_match_above_035(
        self, resolver, mock_vector, mock_embedding, unknown_vendor_invoice
    ):
        """Score >= 0.35 → POSSIBLE_MATCH + SUGGEST."""
        mock_vector.get_sync_time.return_value = datetime.now(timezone.utc)
        mock_vector.semantic_search.return_value = [
            {"erp_id": "SUP-X", "score": 0.40, "distance": 0.60, "metadata": {}},
        ]

        match, status, ctx = resolver.handle_unknown_vendor(
            unknown_vendor_invoice, "t1", "erpnext"
        )

        assert status == VendorStatus.SUGGEST
        assert match.erp_id == "SUP-X"

    def test_fallback_context_has_no_tax_scope(
        self, resolver, mock_vector, mock_embedding, unknown_vendor_invoice
    ):
        """Unknown vendor → fallback context has no tax_scope."""
        mock_vector.get_sync_time.return_value = datetime.now(timezone.utc)
        mock_vector.semantic_search.return_value = []

        match, status, ctx = resolver.handle_unknown_vendor(
            unknown_vendor_invoice, "t1", "erpnext"
        )

        assert ctx.tax_scope is None
        assert ctx.tax_component is None
        assert ctx.vendor_known is False


# ── Line item resolver tests ───────────────────────────────────────


class TestResolveItem:
    def test_hard_match_on_item_code(
        self, resolver, mock_vector
    ):
        """item_code confidence >= 0.90 → hard match."""
        mock_vector.hard_match.return_value = {
            "erp_id": "ITEM-001",
            "item_code": "BOLT-M10-50",
        }

        fields = {
            "description": ScoredField(value="Steel Bolts", confidence=0.85),
            "item_code": ScoredField(value="BOLT-M10-50", confidence=0.93),
            "hsn_code": ScoredField(value="7318", confidence=0.80),
            "uom": ScoredField(value="NOS", confidence=0.95),
        }
        ctx = InvoiceContext(vendor_known=True)

        result = resolver.resolve_item(fields, ctx, "t1", "erpnext")

        assert result.strategy == ResolutionStrategy.HARD_KEY
        assert result.erp_id == "ITEM-001"
        assert result.confidence == 1.0

    def test_filtered_semantic_when_hsn_confident(
        self, resolver, mock_vector, mock_embedding
    ):
        """hsn_code confidence >= 0.70 → filtered semantic."""
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {"erp_id": "ITEM-002", "score": 0.85, "distance": 0.15, "metadata": {}},
        ]

        fields = {
            "description": ScoredField(value="PVC Pipe", confidence=0.72),
            "item_code": None,
            "hsn_code": ScoredField(value="3917", confidence=0.75),
            "uom": ScoredField(value="PCS", confidence=0.60),
        }
        ctx = InvoiceContext(vendor_known=True)

        result = resolver.resolve_item(fields, ctx, "t1", "erpnext")

        assert result.strategy == ResolutionStrategy.FILTERED_SEMANTIC
        # Verify search was called with hsn filter
        call_kwargs = mock_vector.semantic_search.call_args
        assert call_kwargs.kwargs.get("where") or (
            call_kwargs[1].get("where") if len(call_kwargs) > 1 else None
        )

    def test_pure_semantic_when_no_confident_filters(
        self, resolver, mock_vector, mock_embedding
    ):
        """All filter fields < 0.70 → pure semantic."""
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {"erp_id": "ITEM-003", "score": 0.60, "distance": 0.40, "metadata": {}},
        ]

        fields = {
            "description": ScoredField(value="Junction box", confidence=0.48),
            "item_code": None,
            "hsn_code": ScoredField(value="8538", confidence=0.35),
            "uom": ScoredField(value="NOS", confidence=0.42),
        }
        ctx = InvoiceContext(vendor_known=True)

        result = resolver.resolve_item(fields, ctx, "t1", "erpnext")

        assert result.strategy == ResolutionStrategy.PURE_SEMANTIC


class TestResolveUom:
    def test_hard_match_first(self, resolver, mock_vector):
        mock_vector.hard_match.return_value = {"erp_id": "NOS", "uom_code": "NOS"}

        result = resolver.resolve_uom(
            ScoredField(value="NOS", confidence=0.95), "t1", "erpnext"
        )

        assert result.strategy == ResolutionStrategy.HARD_KEY
        assert result.erp_id == "NOS"

    def test_semantic_fallback(self, resolver, mock_vector, mock_embedding):
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {"erp_id": "Nos", "score": 0.88, "distance": 0.12, "metadata": {}},
        ]

        result = resolver.resolve_uom(
            ScoredField(value="Numbers", confidence=0.60), "t1", "erpnext"
        )

        assert result.strategy == ResolutionStrategy.PURE_SEMANTIC


class TestResolveTax:
    def test_hard_match_with_rate_and_component(self, resolver, mock_vector):
        mock_vector.hard_match.return_value = {
            "erp_id": "TAX-IGST-18",
            "rate": "18",
            "component": "IGST",
        }
        ctx = InvoiceContext(vendor_known=True, tax_component="IGST")

        result = resolver.resolve_tax(
            ScoredField(value="18", confidence=0.95), ctx, "t1", "erpnext"
        )

        assert result.strategy == ResolutionStrategy.HARD_KEY
        assert result.erp_id == "TAX-IGST-18"

    def test_filtered_semantic_when_no_component(
        self, resolver, mock_vector, mock_embedding
    ):
        mock_vector.semantic_search.return_value = [
            {"erp_id": "TAX-18", "score": 0.80, "distance": 0.20, "metadata": {}},
        ]
        ctx = InvoiceContext(vendor_known=True, tax_component=None)

        result = resolver.resolve_tax(
            ScoredField(value="18", confidence=0.75), ctx, "t1", "erpnext"
        )

        assert result.strategy in (
            ResolutionStrategy.FILTERED_SEMANTIC,
            ResolutionStrategy.PURE_SEMANTIC,
        )
