"""Tests for the Mapper service and API endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.canonical import ScoredField
from app.models.resolution import (
    FKMatch,
    InvoiceContext,
    ResolvedLineItem,
    ResolutionStrategy,
    VendorStatus,
)
from app.models.response import MappingDetail, MappingStatus, MapResponse
from app.services.mapper import MapperService


# ── Health endpoint tests ───────────────────────────────────────────


class TestHealthEndpoints:
    def test_health(self, client: TestClient):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ── Request validation tests ───────────────────────────────────────


class TestMapEndpoint:
    def test_missing_erp_header_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/map",
            json={"invoice": {}, "options": {}},
            headers={"X-Tenant-ID": "test"},
        )
        assert resp.status_code == 422

    def test_missing_tenant_header_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/map",
            json={"invoice": {}, "options": {}},
            headers={"X-ERP-System": "erpnext"},
        )
        assert resp.status_code == 422


# ── Pydantic model validation tests ────────────────────────────────


class TestCanonicalModelValidation:
    def test_sample_invoice_is_valid(self, sample_invoice):
        assert sample_invoice.invoice_number == "INV-2025-0042"
        assert len(sample_invoice.line_items) == 1

    def test_scored_field_bounds(self):
        with pytest.raises(Exception):
            ScoredField(value="x", confidence=1.5)

        with pytest.raises(Exception):
            ScoredField(value="x", confidence=-0.1)

    def test_all_fixtures_parse(self, all_sample_invoices):
        assert len(all_sample_invoices) == 5
        for name, inv in all_sample_invoices.items():
            assert inv.invoice_number
            assert len(inv.line_items) >= 1


# ── MapperService._build_mappings tests ─────────────────────────────


class TestBuildMappings:
    @pytest.fixture
    def mapper(self, settings):
        with patch(
            "app.services.mapper.VectorService"
        ) as MockVS, patch(
            "app.services.mapper.EmbeddingService"
        ) as MockES:
            MockVS.return_value = MagicMock()
            MockES.return_value = MagicMock()
            return MapperService(settings)

    def test_auto_map_above_threshold(self, mapper):
        vendor_match = FKMatch(
            erp_id="V1",
            strategy=ResolutionStrategy.HARD_KEY,
            confidence=1.0,
        )
        line = ResolvedLineItem(
            index=0,
            item=FKMatch(erp_id="I1", strategy=ResolutionStrategy.HARD_KEY, confidence=1.0),
            uom=FKMatch(erp_id="U1", strategy=ResolutionStrategy.HARD_KEY, confidence=1.0),
            tax=FKMatch(erp_id="T1", strategy=ResolutionStrategy.HARD_KEY, confidence=1.0),
            raw={"description": "test", "quantity": 1, "unit_price": 10},
        )

        mappings, unresolved, review = mapper._build_mappings(
            vendor_match, VendorStatus.FOUND, [line], 0.88
        )

        assert mappings["vendor_name"].status == MappingStatus.AUTO_MAP
        assert mappings["line_items[0].description"].status == MappingStatus.AUTO_MAP
        assert len(unresolved) == 0
        assert len(review) == 0

    def test_suggest_between_thresholds(self, mapper):
        vendor_match = FKMatch(
            erp_id="V1",
            strategy=ResolutionStrategy.PURE_SEMANTIC,
            confidence=0.75,
        )

        mappings, unresolved, review = mapper._build_mappings(
            vendor_match, VendorStatus.SUGGEST, [], 0.88
        )

        assert mappings["vendor_name"].status == MappingStatus.SUGGEST
        assert "SUGGEST" in mappings["vendor_name"].flags

    def test_no_match_flags_unresolved(self, mapper):
        vendor_match = FKMatch(
            strategy=ResolutionStrategy.NOT_FOUND,
            confidence=0.0,
        )

        mappings, unresolved, review = mapper._build_mappings(
            vendor_match, VendorStatus.NOT_FOUND, [], 0.88
        )

        assert mappings["vendor_name"].status == MappingStatus.NO_MATCH
        assert "vendor_name" in unresolved
        assert "VENDOR_NOT_FOUND" in mappings["vendor_name"].flags

    def test_review_required_mid_confidence(self, mapper):
        vendor_match = FKMatch(
            erp_id="V1",
            strategy=ResolutionStrategy.PURE_SEMANTIC,
            confidence=0.55,
        )
        line = ResolvedLineItem(
            index=0,
            item=FKMatch(erp_id="I1", strategy=ResolutionStrategy.PURE_SEMANTIC, confidence=0.60),
            uom=FKMatch(erp_id="U1", strategy=ResolutionStrategy.HARD_KEY, confidence=1.0),
            tax=FKMatch(strategy=ResolutionStrategy.NOT_FOUND, confidence=0.0),
            raw={},
        )

        mappings, unresolved, review = mapper._build_mappings(
            vendor_match, VendorStatus.REVIEW, [line], 0.88
        )

        assert "vendor_name" in review
        assert "line_items[0].description" in review
        assert "line_items[0].tax_rate" in unresolved


# ── Response model tests ────────────────────────────────────────────


class TestMapResponse:
    def test_success_response_shape(self):
        resp = MapResponse(
            status="success",
            erp_payload={"bill_no": "INV-001"},
            mappings={
                "vendor_name": MappingDetail(
                    status=MappingStatus.AUTO_MAP,
                    erp_id="SUP-001",
                    confidence=1.0,
                    strategy="hard_key",
                ),
            },
        )
        assert resp.status == "success"
        assert resp.erp_payload is not None
        assert resp.mappings["vendor_name"].confidence == 1.0

    def test_partial_response(self):
        resp = MapResponse(
            status="partial",
            erp_payload=None,
            mappings={},
            unresolved=["vendor_name"],
            review_required=["line_items[0].description"],
        )
        assert len(resp.unresolved) == 1
        assert len(resp.review_required) == 1
