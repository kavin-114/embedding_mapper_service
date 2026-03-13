"""Tests for the backtest UI endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app


def _make_app(settings: Settings | None = None):
    get_settings.cache_clear()
    app = create_app()
    if settings:
        app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_serve_backtest_page():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/backtest")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Backtest" in resp.text


def test_seed_missing_credentials():
    settings = Settings(erpnext_url="", erpnext_api_key="", erpnext_api_secret="")
    app = _make_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/backtest/seed",
        json={"tenant_id": "t1", "erp_system": "erpnext"},
    )
    assert resp.status_code == 200
    # Parse SSE events
    events = _parse_sse(resp.text)
    assert any(e.get("type") == "error" for e in events)


def test_backtest_run_missing_dir():
    settings = Settings(
        erpnext_url="https://erp.example.com",
        erpnext_api_key="key",
        erpnext_api_secret="secret",
    )
    app = _make_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/backtest/run",
        json={"tenant_id": "t1", "invoices_dir": ""},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e.get("type") == "error" and "invoices_dir" in e.get("message", "") for e in events)


def test_backtest_run_nonexistent_dir():
    settings = Settings(
        erpnext_url="https://erp.example.com",
        erpnext_api_key="key",
        erpnext_api_secret="secret",
    )
    app = _make_app(settings)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/backtest/run",
        json={"tenant_id": "t1", "invoices_dir": "/nonexistent/path"},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e.get("type") == "error" and "not found" in e.get("message", "").lower() for e in events)


def test_seed_streams_events():
    """Seed endpoint streams entity_start, entity_done, and complete events."""
    settings = Settings(
        erpnext_url="https://erp.example.com",
        erpnext_api_key="key",
        erpnext_api_secret="secret",
    )
    app = _make_app(settings)

    mock_client = MagicMock()
    # Make all fetchers return empty lists
    for method in [
        "get_suppliers", "get_items", "get_tax_templates", "get_uoms",
        "get_companies", "get_addresses", "get_cost_centers",
        "get_warehouses", "get_purchase_taxes_templates",
    ]:
        getattr(mock_client, method).return_value = []

    with patch("app.services.connectors.erpnext.ERPNextClient", return_value=mock_client), \
         patch("app.services.vector_service.VectorService") as MockVS, \
         patch("app.services.embedding_service.EmbeddingService"):
        MockVS.return_value.upsert.return_value = 0

        client = TestClient(app)
        resp = client.post(
            "/api/v1/backtest/seed",
            json={"tenant_id": "t1", "erp_system": "erpnext", "limit": 5},
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert "entity_start" in types
    assert "entity_done" in types
    assert "complete" in types


def _parse_sse(text: str) -> list[dict]:
    """Parse SSE text into list of data dicts."""
    events = []
    for block in text.strip().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events
