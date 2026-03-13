"""Tests for the review UI endpoints."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app


def _make_app(settings: Settings):
    """Create a test app with overridden settings."""
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@pytest.fixture
def tmp_review_dir(tmp_path):
    """Create a temp dir with sample extractor JSON files."""
    sample = {"supplier": {"value": "Test Vendor", "confidence_score": 90}, "items": []}
    (tmp_path / "invoice_001.json").write_text(json.dumps(sample))
    (tmp_path / "invoice_002.json").write_text(json.dumps(sample))
    (tmp_path / "notes.txt").write_text("not json")
    return tmp_path


@pytest.fixture
def client_with_review_dir(tmp_review_dir):
    settings = Settings(review_files_dir=str(tmp_review_dir))
    app = _make_app(settings)
    return TestClient(app), tmp_review_dir


@pytest.fixture
def client_no_review_dir():
    settings = Settings(
        review_files_dir="",
        erpnext_url="",
        erpnext_api_key="",
        erpnext_api_secret="",
    )
    app = _make_app(settings)
    return TestClient(app)


def test_serve_review_page(client_with_review_dir):
    client, _ = client_with_review_dir
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Invoice Review" in resp.text


def test_list_files(client_with_review_dir):
    client, _ = client_with_review_dir
    resp = client.get("/api/v1/review/files")
    assert resp.status_code == 200
    files = resp.json()
    assert len(files) == 2
    names = [f["name"] for f in files]
    assert "invoice_001.json" in names
    assert "invoice_002.json" in names
    for f in files:
        assert "path" in f
        assert "size" in f
        assert "modified" in f


def test_list_files_no_dir_configured(client_no_review_dir):
    resp = client_no_review_dir.get("/api/v1/review/files")
    assert resp.status_code == 400


def test_read_file(client_with_review_dir):
    client, tmp_dir = client_with_review_dir
    file_path = str(tmp_dir / "invoice_001.json")
    resp = client.get("/api/v1/review/file", params={"path": file_path})
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier"]["value"] == "Test Vendor"


def test_read_file_path_traversal(client_with_review_dir):
    client, _ = client_with_review_dir
    resp = client.get("/api/v1/review/file", params={"path": "/etc/passwd"})
    assert resp.status_code == 403


def test_read_file_not_found(client_with_review_dir):
    client, tmp_dir = client_with_review_dir
    resp = client.get(
        "/api/v1/review/file",
        params={"path": str(tmp_dir / "nonexistent.json")},
    )
    assert resp.status_code == 404


def test_ground_truth_no_credentials(client_no_review_dir):
    resp = client_no_review_dir.get(
        "/api/v1/review/ground-truth",
        params={"bill_no": "INV-001"},
    )
    assert resp.status_code == 400


def test_ground_truth_found():
    mock_pi = {
        "supplier": "Test Supplier",
        "supplier_name": "Test Supplier Ltd",
        "company": "Test Co",
        "items": [
            {
                "item_code": "ITEM-001",
                "item_name": "Widget",
                "uom": "Nos",
                "item_tax_template": "GST 18%",
                "description": "A widget",
            }
        ],
    }

    settings = Settings(
        review_files_dir="",
        erpnext_url="https://erp.example.com",
        erpnext_api_key="key",
        erpnext_api_secret="secret",
    )
    app = _make_app(settings)

    with patch("app.routers.review.ERPNextClient") as MockClient:
        instance = MagicMock()
        instance.get_purchase_invoice_by_bill_no.return_value = mock_pi
        MockClient.return_value = instance

        client = TestClient(app)
        resp = client.get(
            "/api/v1/review/ground-truth",
            params={"bill_no": "INV-001"},
            headers={"X-Tenant-ID": "t1", "X-ERP-System": "erpnext"},
        )

    assert resp.status_code == 200
    gt = resp.json()
    assert gt["vendor_erp_id"] == "Test Supplier"
    assert len(gt["line_items"]) == 1
    assert gt["line_items"][0]["item_code"] == "ITEM-001"


def test_ground_truth_not_found():
    settings = Settings(
        review_files_dir="",
        erpnext_url="https://erp.example.com",
        erpnext_api_key="key",
        erpnext_api_secret="secret",
    )
    app = _make_app(settings)

    with patch("app.routers.review.ERPNextClient") as MockClient:
        instance = MagicMock()
        instance.get_purchase_invoice_by_bill_no.return_value = None
        MockClient.return_value = instance

        client = TestClient(app)
        resp = client.get(
            "/api/v1/review/ground-truth",
            params={"bill_no": "NONEXISTENT"},
            headers={"X-Tenant-ID": "t1", "X-ERP-System": "erpnext"},
        )

    assert resp.status_code == 404
