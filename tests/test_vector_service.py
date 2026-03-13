"""Tests for the VectorService (unit tests with mocked ChromaDB)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.services.vector_service import VectorService, _DISTANCE_TO_SCORE


class TestCollectionNaming:
    def test_format(self):
        assert (
            VectorService.collection_name("vendors", "tenant_a", "erpnext")
            == "vendors__tenant_a__erpnext"
        )

    def test_different_combinations(self):
        assert (
            VectorService.collection_name("items", "tenant_b", "odoo")
            == "items__tenant_b__odoo"
        )
        assert (
            VectorService.collection_name("tax_codes", "t1", "zoho")
            == "tax_codes__t1__zoho"
        )


class TestDistanceToScore:
    def test_zero_distance_is_perfect(self):
        assert _DISTANCE_TO_SCORE(0.0) == 1.0

    def test_one_distance_is_zero(self):
        assert _DISTANCE_TO_SCORE(1.0) == 0.0

    def test_negative_distance_clamped(self):
        assert _DISTANCE_TO_SCORE(1.5) == 0.0

    def test_mid_distance(self):
        assert _DISTANCE_TO_SCORE(0.3) == pytest.approx(0.7)


class TestUpsert:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_upsert_returns_count(self, MockClient):
        mock_collection = MagicMock()
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        records = [
            {"erp_id": "SUP-001", "text": "Acme Supplies", "tax_id": "29ABC", "category": "Raw"},
            {"erp_id": "SUP-002", "text": "Beta Corp", "tax_id": "33XYZ", "category": "Service"},
        ]
        embedding_fn = MagicMock(return_value=[[0.1] * 10, [0.2] * 10])

        count = svc.upsert(
            entity="vendors",
            tenant_id="t1",
            erp_system="erpnext",
            records=records,
            synced_at=datetime.now(timezone.utc),
            embedding_fn=embedding_fn,
        )

        assert count == 2
        mock_collection.upsert.assert_called_once()
        call_kwargs = mock_collection.upsert.call_args
        assert len(call_kwargs.kwargs["ids"]) == 2
        assert len(call_kwargs.kwargs["embeddings"]) == 2

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_upsert_empty_records(self, MockClient):
        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        count = svc.upsert("vendors", "t1", "erpnext", [], datetime.now(timezone.utc), lambda x: x)
        assert count == 0


class TestHardMatch:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_found(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {
            "ids": ["SUP-001"],
            "metadatas": [{"erp_id": "SUP-001", "tax_id": "29ABC"}],
        }
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        result = svc.hard_match("vendors", "t1", "erpnext", {"tax_id": "29ABC"})
        assert result is not None
        assert result["erp_id"] == "SUP-001"

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_not_found(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": [], "metadatas": []}
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        result = svc.hard_match("vendors", "t1", "erpnext", {"tax_id": "NOPE"})
        assert result is None


class TestSemanticSearch:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_returns_scored_results(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [["SUP-001", "SUP-002"]],
            "distances": [[0.1, 0.3]],
            "metadatas": [[
                {"erp_id": "SUP-001", "category": "Raw"},
                {"erp_id": "SUP-002", "category": "Service"},
            ]],
        }
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.semantic_search(
            "vendors", "t1", "erpnext", [0.1] * 10, n_results=2
        )

        assert len(results) == 2
        assert results[0]["score"] == pytest.approx(0.9)
        assert results[1]["score"] == pytest.approx(0.7)
        assert results[0]["erp_id"] == "SUP-001"

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_empty_results(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.query.return_value = {"ids": [[]], "distances": [[]], "metadatas": [[]]}
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.semantic_search("vendors", "t1", "erpnext", [0.1] * 10)
        assert results == []


class TestSearchVendorContext:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_returns_scored_results(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [["v1__item1"]],
            "distances": [[0.2]],
            "metadatas": [[{
                "vendor_erp_id": "SUP-001",
                "vendor_tax_id": "29ABC",
                "item_erp_id": "ITEM-001",
            }]],
        }
        MockClient.return_value.get_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.search_vendor_context("t1", "erpnext", [0.1] * 10, n_results=1)

        assert len(results) == 1
        assert results[0]["score"] == pytest.approx(0.8)
        assert results[0]["metadata"]["vendor_tax_id"] == "29ABC"

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_returns_empty_when_collection_missing(self, MockClient):
        MockClient.return_value.get_collection.side_effect = Exception("not found")

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.search_vendor_context("t1", "erpnext", [0.1] * 10)
        assert results == []

    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_returns_empty_when_no_results(self, MockClient):
        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [[]],
            "distances": [[]],
            "metadatas": [[]],
        }
        MockClient.return_value.get_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        results = svc.search_vendor_context("t1", "erpnext", [0.1] * 10)
        assert results == []


class TestSyncTime:
    @patch("app.services.vector_service.chromadb.HttpClient")
    def test_records_and_retrieves_sync_time(self, MockClient):
        mock_collection = MagicMock()
        MockClient.return_value.get_or_create_collection.return_value = mock_collection

        settings = Settings(chroma_host="localhost", chroma_port=8000)
        svc = VectorService(settings)

        now = datetime.now(timezone.utc)
        embedding_fn = MagicMock(return_value=[[0.1] * 10])
        svc.upsert(
            "vendors", "t1", "erpnext",
            [{"erp_id": "X", "text": "test"}],
            now, embedding_fn,
        )

        assert svc.get_sync_time("vendors", "t1", "erpnext") == now
        assert svc.get_sync_time("vendors", "t1", "odoo") is None
