"""Tests for the backtest module — all unit tests with mocks."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.connectors.erpnext import ERPNextClient
from scripts.backtest.evaluator import (
    BacktestResult, Evaluator, FieldResult, InvoiceResult,
    _normalize_uom,
)
from scripts.backtest.extractor import (
    extract_ground_truth,
    items_to_seed_records,
    suppliers_to_seed_records,
    tax_templates_to_seed_records,
    uoms_to_seed_records,
)
from scripts.backtest.report import save_csv_report, save_json_report


# ── ERPNextClient tests ──────────────────────────────────────────────


class TestERPNextClient:
    def test_auth_header(self):
        client = ERPNextClient("https://test.erpnext.com", "mykey", "mysecret")
        assert client._headers["Authorization"] == "token mykey:mysecret"

    def test_base_url_strips_trailing_slash(self):
        client = ERPNextClient("https://test.erpnext.com/", "k", "s")
        assert client._base == "https://test.erpnext.com"

    @patch("app.services.connectors.erpnext.httpx.get")
    def test_get_suppliers(self, mock_get):
        # First call: list names; second call: get full doc
        list_resp = MagicMock(
            json=lambda: {"data": [{"name": "SUP-001"}]},
            raise_for_status=lambda: None,
        )
        doc_resp = MagicMock(
            json=lambda: {"data": {"name": "SUP-001", "supplier_name": "Test Supplier"}},
            raise_for_status=lambda: None,
        )
        mock_get.side_effect = [list_resp, doc_resp]
        client = ERPNextClient("https://test.erpnext.com", "k", "s")
        result = client.get_suppliers(limit=10)
        assert len(result) == 1
        assert result[0]["name"] == "SUP-001"

    @patch("app.services.connectors.erpnext.httpx.get")
    def test_get_purchase_invoice(self, mock_get):
        pi_data = {
            "name": "PI-001",
            "supplier": "SUP-001",
            "items": [{"item_code": "ITEM-001", "uom": "Nos"}],
        }
        mock_get.return_value = MagicMock(
            json=lambda: {"data": pi_data},
            raise_for_status=lambda: None,
        )
        client = ERPNextClient("https://test.erpnext.com", "k", "s")
        result = client.get_purchase_invoice("PI-001")
        assert result["supplier"] == "SUP-001"
        assert len(result["items"]) == 1


# ── Extractor tests ──────────────────────────────────────────────────


class TestExtractor:
    def test_suppliers_to_seed_records(self):
        suppliers = [
            {
                "name": "SUP-001",
                "supplier_name": "Test Supplier",
                "tax_id": "29ABCDE1234F1Z5",
                "country": "India",
                "gst_state_number": "29",
                "city": "Bangalore",
                "supplier_type": "Company",
                "default_currency": "INR",
                "supplier_group": "Raw Material",
            }
        ]
        records = suppliers_to_seed_records(suppliers)
        assert len(records) == 1
        r = records[0]
        assert r["erp_id"] == "SUP-001"
        assert r["text"] == "Test Supplier"
        assert r["tax_id"] == "29ABCDE1234F1Z5"
        assert r["tax_id_type"] == "GSTIN"
        assert r["country"] == "India"
        assert r["region_code"] == "29"
        assert r["city"] == "Bangalore"
        assert r["category"] == "Raw Material"
        assert r["active"] is True

    def test_suppliers_disabled(self):
        suppliers = [{"name": "SUP-002", "supplier_name": "X", "disabled": True}]
        records = suppliers_to_seed_records(suppliers)
        assert records[0]["active"] is False

    def test_items_to_seed_records(self):
        items = [
            {
                "name": "ITEM-001",
                "item_name": "Steel Rod",
                "item_code": "ITEM-001",
                "description": "10mm steel rod",
                "item_group": "Raw Material",
                "stock_uom": "Kg",
                "gst_hsn_code": "7214",
            }
        ]
        records = items_to_seed_records(items)
        assert len(records) == 1
        r = records[0]
        assert r["erp_id"] == "ITEM-001"
        assert r["text"] == "Steel Rod"
        assert r["item_code"] == "ITEM-001"
        assert r["hsn_code"] == "7214"

    def test_tax_templates_with_taxes_child(self):
        templates = [
            {
                "name": "GST 18%",
                "taxes": [
                    {"tax_rate": 18, "account_head": "IGST - Company"}
                ],
            }
        ]
        records = tax_templates_to_seed_records(templates)
        assert records[0]["rate"] == "18"
        assert records[0]["component"] == "IGST"

    def test_tax_templates_without_child(self):
        templates = [{"name": "GST 5% - CGST_SGST"}]
        records = tax_templates_to_seed_records(templates)
        assert records[0]["erp_id"] == "GST 5% - CGST_SGST"

    def test_uoms_to_seed_records(self):
        uoms = [{"name": "Kg"}, {"name": "Nos"}]
        records = uoms_to_seed_records(uoms)
        assert len(records) == 2
        assert records[0]["erp_id"] == "Kg"
        assert records[0]["uom_code"] == "Kg"

    def test_extract_ground_truth(self):
        pi = {
            "supplier": "SUP-001",
            "supplier_name": "Test Supplier",
            "items": [
                {
                    "item_code": "ITEM-001",
                    "item_name": "Steel Rod",
                    "uom": "Kg",
                    "item_tax_template": "GST 18%",
                    "description": "10mm steel rod",
                },
            ],
        }
        gt = extract_ground_truth(pi)
        assert gt["vendor_erp_id"] == "SUP-001"
        assert gt["vendor_name"] == "Test Supplier"
        assert len(gt["line_items"]) == 1
        assert gt["line_items"][0]["item_code"] == "ITEM-001"
        assert gt["line_items"][0]["uom"] == "Kg"
        assert gt["line_items"][0]["tax_template"] == "GST 18%"


# ── Evaluator tests ──────────────────────────────────────────────────


class TestEvaluator:
    def _make_map_response(
        self,
        vendor_erp_id: str = "SUP-001",
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        mappings: dict[str, Any] = {
            "vendor_name": {
                "erp_id": vendor_erp_id,
                "confidence": 1.0,
                "strategy": "hard_key",
                "status": "auto_map",
            },
        }
        if items:
            for i, item in enumerate(items):
                mappings[f"line_items[{i}].description"] = {
                    "erp_id": item.get("item_code"),
                    "confidence": item.get("confidence", 0.9),
                    "strategy": item.get("strategy", "pure_semantic"),
                    "status": item.get("status", "auto_map"),
                }
                mappings[f"line_items[{i}].uom"] = {
                    "erp_id": item.get("uom"),
                    "confidence": 1.0,
                    "strategy": "hard_key",
                    "status": "auto_map",
                }
                mappings[f"line_items[{i}].tax_rate"] = {
                    "erp_id": item.get("tax_template"),
                    "confidence": 1.0,
                    "strategy": "hard_key",
                    "status": "auto_map",
                }
        return {"mappings": mappings}

    def _make_ground_truth(
        self,
        vendor_erp_id: str = "SUP-001",
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        gt: dict[str, Any] = {
            "vendor_erp_id": vendor_erp_id,
            "vendor_name": "Test",
            "line_items": items or [],
        }
        return gt

    def test_perfect_accuracy(self):
        items = [{"item_code": "I1", "uom": "Kg", "tax_template": "T1"}]
        response = self._make_map_response(items=items)
        gt = self._make_ground_truth(items=items)

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-001")

        assert result.accuracy == 1.0
        assert result.overall_status == "perfect"
        assert len(result.field_results) == 4  # vendor + 3 line item fields

    def test_partial_accuracy(self):
        response_items = [{"item_code": "I1", "uom": "Kg", "tax_template": "T1"}]
        gt_items = [{"item_code": "I2", "uom": "Kg", "tax_template": "T1"}]
        response = self._make_map_response(items=response_items)
        gt = self._make_ground_truth(items=gt_items)

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-002")

        # vendor correct, item wrong, uom correct, tax correct = 3/4 = 0.75
        assert result.accuracy == 0.75
        assert result.overall_status == "partial"

    def test_vendor_mismatch(self):
        response = self._make_map_response(vendor_erp_id="SUP-002")
        gt = self._make_ground_truth(vendor_erp_id="SUP-001")

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-003")

        assert result.accuracy == 0.0
        assert result.overall_status == "poor"

    def test_batch_evaluation(self):
        result1 = InvoiceResult(
            invoice_number="INV-001",
            supplier="Test",
            field_results=[
                FieldResult("vendor_name", "V1", "V1", True, 1.0, "hard_key", "auto_map"),
            ],
        )
        result2 = InvoiceResult(
            invoice_number="INV-002",
            supplier="Test2",
            field_results=[
                FieldResult("vendor_name", "V2", "V3", False, 0.5, "pure_semantic", "suggest"),
            ],
        )

        evaluator = Evaluator()
        batch = evaluator.evaluate_batch([result1, result2])

        assert batch.overall_accuracy == 0.5
        assert len(batch.failures) == 1
        assert batch.by_field_type["vendor"]["accuracy"] == 0.5
        assert "hard_key" in batch.by_strategy
        assert "pure_semantic" in batch.by_strategy

    def test_empty_ground_truth_skipped(self):
        """Fields with empty expected values are skipped, not penalized."""
        response_items = [{"item_code": "I1", "uom": "Kg", "tax_template": "VAT 5%"}]
        gt_items = [{"item_code": "I1", "uom": "Kg", "tax_template": ""}]
        response = self._make_map_response(items=response_items)
        gt = self._make_ground_truth(items=gt_items)

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-SKIP")

        # tax_template is empty → skipped; vendor + item + uom all correct → 3/3
        assert result.accuracy == 1.0
        tax_field = [f for f in result.field_results if "tax" in f.field_name][0]
        assert tax_field.skipped is True

    def test_none_ground_truth_skipped(self):
        """None ground truth values are also skipped."""
        response_items = [{"item_code": "I1", "uom": "Kg", "tax_template": "T1"}]
        gt_items = [{"item_code": "I1", "uom": "Kg"}]  # no tax_template key
        response = self._make_map_response(items=response_items)
        gt = self._make_ground_truth(items=gt_items)

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-NONE")

        assert result.accuracy == 1.0

    def test_uom_alias_normalization(self):
        """Each and Nos should be treated as equivalent."""
        response_items = [{"item_code": "I1", "uom": "Nos", "tax_template": "T1"}]
        gt_items = [{"item_code": "I1", "uom": "Each", "tax_template": "T1"}]
        response = self._make_map_response(items=response_items)
        gt = self._make_ground_truth(items=gt_items)

        evaluator = Evaluator()
        result = evaluator.evaluate_invoice(response, gt, "INV-UOM")

        uom_field = [f for f in result.field_results if "uom" in f.field_name][0]
        assert uom_field.correct is True
        assert result.accuracy == 1.0

    def test_skipped_excluded_from_batch_aggregations(self):
        """Skipped fields don't appear in batch breakdowns."""
        inv = InvoiceResult(
            invoice_number="INV-001",
            supplier="Test",
            field_results=[
                FieldResult("vendor_name", "V1", "V1", True, 1.0, "hard_key", "auto_map"),
                FieldResult("line_items[0].tax_rate", "", "T1", True, 0.9, "semantic", "auto_map", skipped=True),
            ],
        )
        evaluator = Evaluator()
        batch = evaluator.evaluate_batch([inv])

        assert batch.overall_accuracy == 1.0
        assert "tax" not in batch.by_field_type
        assert len(batch.failures) == 0


class TestUomNormalize:
    def test_each_to_nos(self):
        assert _normalize_uom("Each") == "Nos"
        assert _normalize_uom("each") == "Nos"

    def test_nos_stays(self):
        assert _normalize_uom("Nos") == "Nos"

    def test_kg_variants(self):
        assert _normalize_uom("kgs") == "Kg"
        assert _normalize_uom("Kilogram") == "Kg"

    def test_unknown_passthrough(self):
        assert _normalize_uom("CustomUnit") == "CustomUnit"

    def test_none_passthrough(self):
        assert _normalize_uom(None) is None


# ── Report tests ─────────────────────────────────────────────────────


class TestReport:
    @pytest.fixture
    def sample_result(self) -> BacktestResult:
        return BacktestResult(
            invoice_results=[
                InvoiceResult(
                    invoice_number="INV-001",
                    supplier="Test Supplier",
                    field_results=[
                        FieldResult("vendor_name", "V1", "V1", True, 1.0, "hard_key", "auto_map"),
                        FieldResult(
                            "line_items[0].description", "I1", "I1", True, 0.92,
                            "pure_semantic", "auto_map",
                        ),
                        FieldResult(
                            "line_items[0].uom", "Kg", "Nos", False, 0.8,
                            "hard_key", "auto_map",
                        ),
                    ],
                ),
            ],
        )

    def test_save_json_report(self, sample_result):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_json_report(sample_result, f"{tmpdir}/test_report")
            assert Path(path).exists()
            with open(path) as f:
                data = json.load(f)
            assert "overall_accuracy" in data
            assert data["invoice_count"] == 1
            assert len(data["invoices"]) == 1

    def test_save_csv_report(self, sample_result):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_csv_report(sample_result, f"{tmpdir}/test_report")
            assert Path(path).exists()
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            assert len(rows) == 3
            assert rows[0]["invoice_number"] == "INV-001"
            assert rows[0]["field_name"] == "vendor_name"
            assert rows[0]["correct"] == "True"

    def test_backtest_result_to_dict(self, sample_result):
        d = sample_result.to_dict()
        assert d["overall_accuracy"] == pytest.approx(0.6667, abs=0.01)
        assert "vendor" in d["by_field_type"]
        assert len(d["failures"]) == 1
        assert d["failures"][0]["field_name"] == "line_items[0].uom"
