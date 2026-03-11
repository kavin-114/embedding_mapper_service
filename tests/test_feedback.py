"""Tests for the feedback loop — vendor context learning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.models.feedback import ApprovedLineItem, FeedbackRequest, FeedbackResponse
from app.models.resolution import InvoiceContext
from app.services.context_builder import ContextBuilder


# ── Feedback model tests ────────────────────────────────────────────


class TestFeedbackModels:
    def test_feedback_request_valid(self):
        req = FeedbackRequest(
            tenant_id="tenant_a",
            erp_system="erpnext",
            invoice_number="INV-001",
            vendor_erp_id="Tata Steel Trading Co",
            vendor_name="Tata Steel Trading Co",
            vendor_tax_id="27AAACT2727Q1ZV",
            line_items=[
                ApprovedLineItem(
                    description="HR Coil 2.0mm",
                    item_erp_id="HR-COIL-2MM-E250",
                    item_code="HR-COIL-2MM-E250",
                    hsn_code="7208",
                    uom="MT",
                ),
            ],
        )
        assert req.vendor_erp_id == "Tata Steel Trading Co"
        assert len(req.line_items) == 1

    def test_feedback_response_shape(self):
        resp = FeedbackResponse(
            status="ok",
            records_upserted=2,
            vendor_erp_id="SUP-001",
            collection="vendor_context__tenant_a__erpnext",
        )
        assert resp.records_upserted == 2


# ── Feedback API endpoint tests ─────────────────────────────────────


class TestFeedbackEndpoint:
    def test_feedback_endpoint_exists(self, client):
        """Verify the /api/v1/feedback route is registered."""
        routes = [r.path for r in client.app.routes if hasattr(r, "path")]
        assert "/api/v1/feedback" in routes

    def test_feedback_validation(self, client):
        """Missing required fields → 422."""
        resp = client.post("/api/v1/feedback", json={"tenant_id": "x"})
        assert resp.status_code == 422


# ── VectorService vendor_context tests ──────────────────────────────


class TestVendorContextStorage:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_upsert_vendor_context(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": [], "metadatas": []}
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        from app.services.vector_service import VectorService

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        items = [
            {
                "item_erp_id": "HR-COIL-2MM-E250",
                "item_code": "HR-COIL-2MM-E250",
                "hsn_code": "7208",
                "uom": "MT",
                "description": "HR Coil 2.0mm",
            },
            {
                "item_erp_id": "HR-COIL-3MM-E250",
                "item_code": "HR-COIL-3MM-E250",
                "hsn_code": "7208",
                "uom": "MT",
                "description": "HR Coil 3.0mm",
            },
        ]
        embedding_fn = MagicMock(return_value=[[0.1] * 10, [0.2] * 10])

        count = svc.upsert_vendor_context(
            tenant_id="tenant_a",
            erp_system="erpnext",
            vendor_erp_id="Tata Steel Trading Co",
            vendor_name="Tata Steel Trading Co",
            vendor_tax_id="27AAACT2727Q1ZV",
            items=items,
            embedding_fn=embedding_fn,
        )

        assert count == 2
        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args.kwargs
        # IDs should be vendor__item
        assert "Tata Steel Trading Co__HR-COIL-2MM-E250" in call_kwargs["ids"]
        # Metadata should include vendor_tax_id
        assert call_kwargs["metadatas"][0]["vendor_tax_id"] == "27AAACT2727Q1ZV"
        assert call_kwargs["metadatas"][0]["frequency"] == 1

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_frequency_increments_on_repeat(self, MockClient):
        """Second feedback for same vendor+item → frequency 2."""
        mock_collection = MagicMock()
        # Simulate existing record with frequency=1
        mock_collection.get.return_value = {
            "ids": ["V1__I1"],
            "metadatas": [{"frequency": 1, "vendor_erp_id": "V1"}],
        }
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        from app.services.vector_service import VectorService

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        embedding_fn = MagicMock(return_value=[[0.1] * 10])
        svc.upsert_vendor_context(
            tenant_id="t1",
            erp_system="erpnext",
            vendor_erp_id="V1",
            vendor_name="Vendor One",
            vendor_tax_id=None,
            items=[{"item_erp_id": "I1", "description": "Item One"}],
            embedding_fn=embedding_fn,
        )

        call_kwargs = mock_collection.upsert.call_args.kwargs
        assert call_kwargs["metadatas"][0]["frequency"] == 2

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_get_vendor_context_returns_sorted(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {
            "ids": ["V1__I1", "V1__I2", "V1__I3"],
            "metadatas": [
                {"item_erp_id": "I1", "frequency": 2, "vendor_erp_id": "V1"},
                {"item_erp_id": "I2", "frequency": 5, "vendor_erp_id": "V1"},
                {"item_erp_id": "I3", "frequency": 1, "vendor_erp_id": "V1"},
            ],
        }
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        from app.services.vector_service import VectorService

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.get_vendor_context("t1", "erpnext", "V1")

        assert len(results) == 3
        # Should be sorted by frequency descending
        assert results[0]["item_erp_id"] == "I2"  # freq 5
        assert results[1]["item_erp_id"] == "I1"  # freq 2
        assert results[2]["item_erp_id"] == "I3"  # freq 1

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_get_vendor_context_empty(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": [], "metadatas": []}
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        from app.services.vector_service import VectorService

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.get_vendor_context("t1", "erpnext", "UNKNOWN")
        assert results == []


# ── Context builder with vendor history tests ───────────────────────


class TestContextBuilderWithHistory:
    def test_build_with_preferred_items(self):
        settings = Settings(company_country="IN", company_region_code="29")
        builder = ContextBuilder(settings)

        mock_vector = MagicMock()
        mock_vector.get_vendor_context.return_value = [
            {
                "item_erp_id": "HR-COIL-2MM-E250",
                "item_code": "HR-COIL-2MM-E250",
                "hsn_code": "7208",
                "description": "HR Coil 2.0mm",
                "frequency": 5,
                "vendor_tax_id": "27AAACT2727Q1ZV",
            },
            {
                "item_erp_id": "HR-COIL-3MM-E250",
                "item_code": "HR-COIL-3MM-E250",
                "hsn_code": "7208",
                "description": "HR Coil 3.0mm",
                "frequency": 2,
            },
        ]

        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "27", "category": "Raw Material"},
            vendor_erp_id="Tata Steel Trading Co",
            vendor_confidence=0.95,
            vector_svc=mock_vector,
            tenant_id="tenant_a",
            erp_system="erpnext",
        )

        assert len(ctx.preferred_items) == 2
        assert ctx.preferred_items[0]["item_erp_id"] == "HR-COIL-2MM-E250"
        assert ctx.preferred_items[0]["frequency"] == 5
        assert ctx.verified_tax_id == "27AAACT2727Q1ZV"
        assert ctx.tax_scope == "INTER_REGION"  # region 27 != company 29

    def test_build_without_history_still_works(self):
        settings = Settings(company_country="IN", company_region_code="29")
        builder = ContextBuilder(settings)

        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29"},
            vendor_erp_id="SUP-001",
            vendor_confidence=0.90,
        )

        assert ctx.preferred_items == []
        assert ctx.verified_tax_id is None
        assert ctx.tax_scope == "INTRA_REGION"

    def test_build_with_empty_history(self):
        settings = Settings(company_country="IN", company_region_code="29")
        builder = ContextBuilder(settings)

        mock_vector = MagicMock()
        mock_vector.get_vendor_context.return_value = []

        ctx = builder.build(
            vendor_metadata={"country": "IN", "region_code": "29"},
            vendor_erp_id="SUP-NEW",
            vendor_confidence=0.90,
            vector_svc=mock_vector,
            tenant_id="t1",
            erp_system="erpnext",
        )

        assert ctx.preferred_items == []
        assert ctx.verified_tax_id is None


# ── Resolver preferred_items boost tests ────────────────────────────


class TestResolverHistoryBoost:
    def test_preferred_item_gets_boosted(self):
        """Items in vendor history get +0.10 score boost."""
        from app.services.resolver import Resolver

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        mock_embed = MagicMock()
        mock_embed.encode.return_value = [[0.1] * 384]
        mock_vector = MagicMock()

        # Item not in preferred_items is top result at 0.85
        # Item in preferred_items is second at 0.80
        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {"erp_id": "ITEM-OTHER", "score": 0.85, "distance": 0.15, "metadata": {}},
            {"erp_id": "ITEM-PREFERRED", "score": 0.80, "distance": 0.20, "metadata": {}},
        ]

        resolver = Resolver(settings, mock_embed, mock_vector)

        context = InvoiceContext(
            vendor_known=True,
            preferred_items=[
                {"item_erp_id": "ITEM-PREFERRED", "item_code": "X", "frequency": 3,
                 "hsn_code": None, "description": "Preferred Item"},
            ],
        )

        from app.models.canonical import ScoredField

        result = resolver.resolve_item(
            {
                "description": ScoredField(value="Some item", confidence=0.80),
                "item_code": None,
                "hsn_code": None,
                "uom": None,
            },
            context,
            "t1",
            "erpnext",
        )

        # ITEM-PREFERRED gets boosted: 0.80 + 0.10 = 0.90 > 0.85
        assert result.erp_id == "ITEM-PREFERRED"
        assert result.confidence == pytest.approx(0.90, abs=0.01)

    def test_no_boost_without_preferred_items(self):
        """Without preferred_items, no boost applied."""
        from app.services.resolver import Resolver

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        mock_embed = MagicMock()
        mock_embed.encode.return_value = [[0.1] * 384]
        mock_vector = MagicMock()

        mock_vector.hard_match.return_value = None
        mock_vector.semantic_search.return_value = [
            {"erp_id": "ITEM-A", "score": 0.85, "distance": 0.15, "metadata": {}},
        ]

        resolver = Resolver(settings, mock_embed, mock_vector)
        context = InvoiceContext(vendor_known=True, preferred_items=[])

        from app.models.canonical import ScoredField

        result = resolver.resolve_item(
            {"description": ScoredField(value="Some item", confidence=0.80),
             "item_code": None, "hsn_code": None, "uom": None},
            context, "t1", "erpnext",
        )

        assert result.erp_id == "ITEM-A"
        assert result.confidence == pytest.approx(0.85, abs=0.01)
