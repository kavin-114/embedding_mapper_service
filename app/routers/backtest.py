"""Backtest UI endpoints — seed ChromaDB and run backtests with SSE streaming."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["backtest"])

STATIC_DIR = Path(__file__).parent.parent / "static"


# ── Request models ────────────────────────────────────────


class SeedRequest(BaseModel):
    erp_url: str = ""
    api_key: str = ""
    api_secret: str = ""
    tenant_id: str = "alfarsi"
    erp_system: str = "erpnext"
    limit: int = 0


class BacktestRunRequest(BaseModel):
    erp_url: str = ""
    api_key: str = ""
    api_secret: str = ""
    tenant_id: str = "alfarsi"
    erp_system: str = "erpnext"
    invoices_dir: str = ""
    format: str = "extractor"
    invoice_map: str | None = None


# ── SSE helpers ───────────────────────────────────────────


def _sse(data: dict[str, Any], event: str = "message") -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── Serve page ────────────────────────────────────────────


@router.get("/backtest")
async def serve_backtest_page() -> FileResponse:
    html_path = STATIC_DIR / "backtest.html"
    return FileResponse(html_path, media_type="text/html")


@router.get("/api/v1/backtest/config")
async def get_backtest_config(
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return configured .env defaults for the backtest UI form.

    API key and secret are masked — the backend falls back to .env
    values when the form fields are empty, so the UI only needs to
    indicate that credentials are configured.
    """
    def _mask(val: str) -> str:
        if not val:
            return ""
        if len(val) <= 6:
            return "*" * len(val)
        return val[:3] + "*" * (len(val) - 6) + val[-3:]

    return {
        "erpnext_url": settings.erpnext_url,
        "erpnext_api_key": _mask(settings.erpnext_api_key),
        "erpnext_api_secret": _mask(settings.erpnext_api_secret),
        "review_files_dir": settings.review_files_dir,
    }


# ── Seed endpoint (SSE) ──────────────────────────────────


@router.post("/api/v1/backtest/seed")
async def seed_chromadb(
    body: SeedRequest,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def generate() -> AsyncGenerator[str, None]:
        from app.services.connectors.erpnext import ERPNextClient
        from app.services.connectors.erpnext_extractors import ENTITY_SYNC_MAP
        from app.services.embedding_service import EmbeddingService
        from app.services.vector_service import VectorService

        erp_url = body.erp_url or settings.erpnext_url
        api_key = body.api_key or settings.erpnext_api_key
        api_secret = body.api_secret or settings.erpnext_api_secret

        if not erp_url or not api_key:
            yield _sse({"type": "error", "message": "ERPNext credentials required"}, "seed")
            return

        client = ERPNextClient(erp_url, api_key, api_secret)
        embedding_svc = EmbeddingService(settings)
        vector_svc = VectorService(settings)
        now = datetime.now(timezone.utc)

        entities = list(ENTITY_SYNC_MAP.keys())
        total = len(entities)
        total_upserted = 0
        t0 = time.time()

        for i, entity in enumerate(entities):
            yield _sse({
                "type": "entity_start", "entity": entity,
                "index": i, "total": total,
            }, "seed")

            method_name, converter = ENTITY_SYNC_MAP[entity]
            try:
                fetcher = getattr(client, method_name)
                raw_docs = await asyncio.to_thread(fetcher, limit=body.limit)
                records = converter(raw_docs)
                count = await asyncio.to_thread(
                    vector_svc.upsert,
                    entity=entity,
                    tenant_id=body.tenant_id,
                    erp_system=body.erp_system,
                    records=records,
                    synced_at=now,
                    embedding_fn=embedding_svc.encode,
                )
                total_upserted += count
                yield _sse({
                    "type": "entity_done", "entity": entity,
                    "fetched": len(raw_docs), "upserted": count,
                    "index": i, "total": total,
                }, "seed")
            except Exception as e:
                logger.error("backtest.seed_error", entity=entity, error=str(e))
                yield _sse({
                    "type": "entity_error", "entity": entity,
                    "error": str(e), "index": i, "total": total,
                }, "seed")

        yield _sse({
            "type": "complete",
            "total_upserted": total_upserted,
            "duration_s": round(time.time() - t0, 1),
        }, "seed")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Backtest run endpoint (SSE) ───────────────────────────


@router.post("/api/v1/backtest/run")
async def run_backtest(
    body: BacktestRunRequest,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def generate() -> AsyncGenerator[str, None]:
        from app.services.connectors.erpnext import ERPNextClient
        from app.services.mapper import MapperService
        from scripts.backtest.evaluator import Evaluator, InvoiceResult
        from scripts.backtest.extractor import extract_ground_truth
        from scripts.backtest.run import (
            _load_canonical_invoices,
            _load_extractor_invoices,
            _load_invoice_map,
        )

        erp_url = body.erp_url or settings.erpnext_url
        api_key = body.api_key or settings.erpnext_api_key
        api_secret = body.api_secret or settings.erpnext_api_secret

        if not erp_url or not api_key:
            yield _sse({"type": "error", "message": "ERPNext credentials required"}, "backtest")
            return

        if not body.invoices_dir:
            yield _sse({"type": "error", "message": "invoices_dir is required"}, "backtest")
            return

        invoices_path = Path(body.invoices_dir)
        if not invoices_path.is_dir():
            yield _sse({"type": "error", "message": f"Directory not found: {body.invoices_dir}"}, "backtest")
            return

        # Load invoices
        try:
            if body.format == "extractor":
                invoice_map = _load_invoice_map(body.invoice_map)
                if not invoice_map:
                    yield _sse({"type": "error", "message": "invoice_map required for extractor format"}, "backtest")
                    return
                pairs = _load_extractor_invoices(body.invoices_dir, invoice_map)
            else:
                canonical = _load_canonical_invoices(body.invoices_dir)
                pairs = [(inv, inv.invoice_number) for inv in canonical]
        except SystemExit:
            yield _sse({"type": "error", "message": "Failed to load invoices"}, "backtest")
            return

        total = len(pairs)
        yield _sse({"type": "loaded", "count": total, "format": body.format}, "backtest")

        # Initialize services
        mapper_svc = MapperService(settings)
        erp_client = ERPNextClient(erp_url, api_key, api_secret)
        evaluator = Evaluator()

        class _Opts:
            confidence_threshold = 0.88
            return_candidates = True
            dry_run = False

        options = _Opts()
        invoice_results: list[InvoiceResult] = []

        for done_idx, (inv, pi_name) in enumerate(pairs):
            label = f"{inv.invoice_number} ({pi_name})" if pi_name != inv.invoice_number else inv.invoice_number

            # Fetch ground truth
            try:
                pi = await asyncio.to_thread(erp_client.get_purchase_invoice, pi_name)
                gt = extract_ground_truth(pi)
            except Exception as e:
                yield _sse({
                    "type": "invoice_error", "label": label,
                    "error": str(e), "done": done_idx + 1, "total": total,
                }, "backtest")
                continue

            # Run mapper
            try:
                response = await asyncio.to_thread(
                    mapper_svc.map, inv, body.erp_system, body.tenant_id, options,
                )
                response_dict = response.model_dump()
            except Exception as e:
                yield _sse({
                    "type": "invoice_error", "label": label,
                    "error": f"Mapping failed: {e}", "done": done_idx + 1, "total": total,
                }, "backtest")
                continue

            # Evaluate
            inv_result = evaluator.evaluate_invoice(response_dict, gt, label)
            invoice_results.append(inv_result)

            yield _sse({
                "type": "invoice",
                "label": label,
                "accuracy": round(inv_result.accuracy, 4),
                "status_icon": "+" if inv_result.accuracy >= 0.8 else "-",
                "overall_status": inv_result.overall_status,
                "supplier": inv_result.supplier,
                "done": done_idx + 1,
                "total": total,
            }, "backtest")

        # Summary
        if invoice_results:
            batch = evaluator.evaluate_batch(invoice_results)
            summary = batch.to_dict()
            summary["type"] = "summary"
            yield _sse(summary, "backtest")
        else:
            yield _sse({"type": "summary", "overall_accuracy": 0, "invoice_count": 0}, "backtest")

    return StreamingResponse(generate(), media_type="text/event-stream")
