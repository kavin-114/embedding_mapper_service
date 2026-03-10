"""Tests for the Transformer service."""

from __future__ import annotations

import pytest

from app.services.transformer import Transformer


@pytest.fixture
def erpnext_transformer():
    return Transformer("erpnext")


@pytest.fixture
def odoo_transformer():
    return Transformer("odoo")


@pytest.fixture
def zoho_transformer():
    return Transformer("zoho")


# ── Schema loading tests ───────────────────────────────────────────


class TestSchemaLoading:
    def test_erpnext_loads(self, erpnext_transformer):
        assert erpnext_transformer.get_field_map()["invoice_number"] == "bill_no"
        assert erpnext_transformer.get_id_type() == "name_string"

    def test_odoo_loads(self, odoo_transformer):
        assert odoo_transformer.get_field_map()["invoice_number"] == "ref"
        assert odoo_transformer.get_id_type() == "integer"

    def test_zoho_loads(self, zoho_transformer):
        assert zoho_transformer.get_field_map()["invoice_number"] == "bill_number"
        assert zoho_transformer.get_id_type() == "long_string"

    def test_invalid_erp_raises(self):
        with pytest.raises(FileNotFoundError):
            Transformer("nonexistent_erp")

    def test_line_items_key(self, erpnext_transformer, odoo_transformer):
        assert erpnext_transformer.get_line_items_key() == "items"
        assert odoo_transformer.get_line_items_key() == "invoice_line_ids"


# ── Transform tests ───────────────────────────────────────────────


class TestTransformERPNext:
    def test_basic_transform(self, erpnext_transformer):
        canonical = {
            "invoice_number": "INV-001",
            "invoice_date": "2025-07-10",
            "currency": "INR",
            "total_amount": 11800.00,
        }
        resolved_ids = {"vendor_name": "Acme Supplies Pvt Ltd"}
        lines = [
            {
                "resolved_ids": {
                    "description": "Steel Bolts M10x50",
                    "uom": "NOS",
                    "tax_rate": "Output Tax IGST - 18%",
                },
                "raw": {"quantity": 100, "unit_price": 100.00},
            }
        ]

        result = erpnext_transformer.transform(canonical, resolved_ids, lines)

        assert result["bill_no"] == "INV-001"
        assert result["bill_date"] == "2025-07-10"
        assert result["supplier"] == "Acme Supplies Pvt Ltd"
        assert result["currency"] == "INR"
        assert result["grand_total"] == 11800.00
        assert len(result["items"]) == 1
        assert result["items"][0]["item_name"] == "Steel Bolts M10x50"
        assert result["items"][0]["qty"] == 100
        assert result["items"][0]["rate"] == 100.00
        assert result["items"][0]["uom"] == "NOS"


class TestTransformOdoo:
    def test_odoo_orm_tuples_m2m(self, odoo_transformer):
        """tax_ids should be wrapped as [(6, 0, [id])]."""
        canonical = {
            "invoice_number": "INV-002",
            "invoice_date": "2025-07-10",
            "currency": "INR",
            "total_amount": 5000.00,
        }
        resolved_ids = {"vendor_name": 42}
        lines = [
            {
                "resolved_ids": {
                    "description": 101,
                    "uom": 5,
                    "tax_rate": 7,
                },
                "raw": {"quantity": 10, "unit_price": 500.00},
            }
        ]

        result = odoo_transformer.transform(canonical, resolved_ids, lines)

        # partner_id is m2o → plain int
        assert result["partner_id"] == 42
        # product_uom_id is m2o → plain int
        assert result["invoice_line_ids"][0]["product_uom_id"] == 5
        # tax_ids is M2M → [(6, 0, [7])]
        assert result["invoice_line_ids"][0]["tax_ids"] == [(6, 0, [7])]

    def test_odoo_id_casting(self, odoo_transformer):
        """Odoo IDs should be cast to integers."""
        canonical = {"invoice_number": "X", "invoice_date": "2025-01-01",
                     "currency": "INR", "total_amount": 100}
        resolved_ids = {"vendor_name": "42"}  # string "42"
        result = odoo_transformer.transform(canonical, resolved_ids, [])

        assert result["partner_id"] == 42
        assert isinstance(result["partner_id"], int)


class TestTransformZoho:
    def test_zoho_field_names(self, zoho_transformer):
        canonical = {
            "invoice_number": "INV-003",
            "invoice_date": "2025-08-01",
            "currency": "INR",
            "total_amount": 8000.00,
        }
        resolved_ids = {"vendor_name": "123456789012345"}
        lines = [
            {
                "resolved_ids": {"description": "ITEM-ZB-001", "uom": "kg", "tax_rate": "TAX-001"},
                "raw": {"quantity": 5, "unit_price": 1600.00},
            }
        ]

        result = zoho_transformer.transform(canonical, resolved_ids, lines)

        assert result["bill_number"] == "INV-003"
        assert result["vendor_id"] == "123456789012345"
        assert result["line_items"][0]["name"] == "ITEM-ZB-001"
        assert result["line_items"][0]["unit"] == "kg"
