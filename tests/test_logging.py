"""Tests for structured logging, metrics, and middleware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from app.logging_config import setup_logging, get_logger
from app.metrics import PipelineMetrics
from app.models.resolution import (
    FKMatch,
    InvoiceContext,
    ResolvedLineItem,
    ResolutionStrategy,
    VendorStatus,
)
from app.models.response import MappingStatus
from app.services.mapper import MapperService


# ── setup_logging tests ──────────────────────────────────────────────


class TestSetupLogging:
    def test_json_format(self):
        setup_logging(log_format="json", log_level="INFO")
        log = get_logger("test")
        assert log is not None

    def test_dev_format(self):
        setup_logging(log_format="dev", log_level="DEBUG")
        log = get_logger("test.dev")
        assert log is not None

    def test_invalid_level_defaults_to_info(self):
        setup_logging(log_format="json", log_level="INVALID")
        log = get_logger("test.invalid")
        assert log is not None


# ── PipelineMetrics tests ────────────────────────────────────────────


class TestPipelineMetrics:
    def test_initial_counts_are_zero(self):
        m = PipelineMetrics()
        assert m.auto_map_count == 0
        assert m.suggest_count == 0
        assert m.review_count == 0
        assert m.no_match_count == 0
        assert m.line_item_count == 0

    def test_count_status_accumulates(self):
        m = PipelineMetrics()
        m.count_status("auto_map")
        m.count_status("auto_map")
        m.count_status("suggest")
        m.count_status("review")
        m.count_status("no_match")

        assert m.auto_map_count == 2
        assert m.suggest_count == 1
        assert m.review_count == 1
        assert m.no_match_count == 1

    def test_add_field_records_details(self):
        m = PipelineMetrics()
        m.add_field("vendor_name", strategy="hard_key", confidence=1.0, status="auto_map", erp_id="V1")
        m.add_field("line_items[0].description", strategy="pure_semantic", confidence=0.85)

        assert len(m.field_details) == 2
        assert m.field_details[0].field_name == "vendor_name"
        assert m.field_details[1].confidence == 0.85

    def test_to_dict_structure(self):
        m = PipelineMetrics()
        m.schema_load_ms = 1.5
        m.vendor_resolution_ms = 10.3
        m.total_ms = 50.0
        m.vendor_strategy = "hard_key"
        m.vendor_confidence = 1.0
        m.vendor_status = "found"
        m.vendor_erp_id = "V1"
        m.line_item_count = 3
        m.auto_map_count = 2
        m.suggest_count = 1

        d = m.to_dict()
        assert d["timings"]["schema_load_ms"] == 1.5
        assert d["timings"]["total_ms"] == 50.0
        assert d["vendor"]["strategy"] == "hard_key"
        assert d["vendor"]["erp_id"] == "V1"
        assert d["line_items"]["count"] == 3
        assert d["line_items"]["auto_map"] == 2
        assert d["line_items"]["suggest"] == 1

    def test_to_dict_field_details(self):
        m = PipelineMetrics()
        m.add_field("vendor_name", strategy="hard_key", confidence=1.0, status="auto_map")
        d = m.to_dict()
        assert len(d["field_details"]) == 1
        assert d["field_details"][0]["field_name"] == "vendor_name"


# ── Mapper logging integration tests ─────────────────────────────────


class TestMapperLogging:
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

    def test_build_mappings_populates_metrics(self, mapper):
        metrics = PipelineMetrics()
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
            vendor_match, VendorStatus.FOUND, [line], 0.88, metrics,
        )

        assert len(metrics.field_details) == 4  # vendor + 3 line item fields
        assert metrics.field_details[0].field_name == "vendor_name"
        assert metrics.field_details[0].strategy == "hard_key"

    def test_build_mappings_works_without_metrics(self, mapper):
        """Backwards compatibility — metrics=None should work."""
        vendor_match = FKMatch(
            erp_id="V1",
            strategy=ResolutionStrategy.HARD_KEY,
            confidence=1.0,
        )
        mappings, unresolved, review = mapper._build_mappings(
            vendor_match, VendorStatus.FOUND, [], 0.88,
        )
        assert "vendor_name" in mappings
