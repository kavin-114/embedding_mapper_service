"""Shared test fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.canonical import CanonicalInvoice, CanonicalLineItem, ScoredField

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_sample_invoices() -> dict[str, Any]:
    with open(FIXTURES_DIR / "sample_invoices.json") as f:
        return json.load(f)


def _parse_invoice(data: dict[str, Any]) -> CanonicalInvoice:
    """Parse a raw dict from the fixture file into a CanonicalInvoice."""
    cleaned = {k: v for k, v in data.items() if k != "_comment"}
    cleaned["line_items"] = [
        {k: v for k, v in li.items() if k != "_comment"}
        for li in cleaned["line_items"]
    ]
    return CanonicalInvoice(**cleaned)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        chroma_host="localhost",
        chroma_port=8000,
        company_state_code="29",
    )


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ── Sample invoices loaded from fixtures/sample_invoices.json ────────

@pytest.fixture
def all_sample_invoices() -> dict[str, CanonicalInvoice]:
    """All five sample invoices keyed by scenario name."""
    raw = _load_sample_invoices()
    return {name: _parse_invoice(data) for name, data in raw.items()}


@pytest.fixture
def sample_invoice(all_sample_invoices) -> CanonicalInvoice:
    """Default test invoice — high confidence, all fields present."""
    return all_sample_invoices["high_confidence_invoice"]


@pytest.fixture
def mixed_confidence_invoice(all_sample_invoices) -> CanonicalInvoice:
    """Invoice with varied confidence — exercises all strategy paths."""
    return all_sample_invoices["mixed_confidence_invoice"]


@pytest.fixture
def unknown_vendor_invoice(all_sample_invoices) -> CanonicalInvoice:
    """Invoice with an unknown vendor — triggers Stage 3 handler."""
    return all_sample_invoices["unknown_vendor_invoice"]


@pytest.fixture
def interstate_invoice(all_sample_invoices) -> CanonicalInvoice:
    """Interstate invoice (TN→KA) — should resolve to IGST."""
    return all_sample_invoices["interstate_invoice"]


@pytest.fixture
def no_gstin_invoice(all_sample_invoices) -> CanonicalInvoice:
    """Invoice with no GSTIN — purely semantic vendor resolution."""
    return all_sample_invoices["no_gstin_invoice"]
