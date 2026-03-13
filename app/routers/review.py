"""Review UI endpoints — serves the review page and supporting APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.services.connectors.erpnext import ERPNextClient

logger = get_logger(__name__)

router = APIRouter(tags=["review"])

STATIC_DIR = Path(__file__).parent.parent / "static"


@router.get("/review")
async def serve_review_page() -> FileResponse:
    """Serve the review HTML page."""
    html_path = STATIC_DIR / "review.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="review.html not found")
    return FileResponse(html_path, media_type="text/html")


@router.get("/api/v1/review/files")
async def list_review_files(
    dir: str = Query("", description="Directory to list (defaults to review_files_dir)"),
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    """List extractor JSON files from a configured directory."""
    base_dir = dir or settings.review_files_dir
    if not base_dir:
        raise HTTPException(status_code=400, detail="No review_files_dir configured and no dir param provided")

    base = Path(base_dir).resolve()
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {base_dir}")

    files = []
    for p in sorted(base.glob("*.json")):
        stat = p.stat()
        files.append({
            "name": p.name,
            "path": str(p),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    return files


@router.get("/api/v1/review/file")
async def read_review_file(
    path: str = Query(..., description="Path to the extractor JSON file"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Read an extractor JSON file from the server. Validates path is under allowed directory."""
    allowed_dir = settings.review_files_dir
    if not allowed_dir:
        raise HTTPException(status_code=400, detail="review_files_dir not configured")

    file_path = Path(path).resolve()
    allowed = Path(allowed_dir).resolve()

    # Prevent path traversal
    if not str(file_path).startswith(str(allowed)):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed directory")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")


@router.get("/api/v1/review/ground-truth")
async def get_ground_truth(
    bill_no: str = Query(..., description="Supplier invoice number to look up"),
    x_tenant_id: Annotated[str, Header()] = "",
    x_erp_system: Annotated[str, Header()] = "",
    settings: Settings = Depends(get_settings),
) -> dict:
    """Fetch ground truth from ERPNext by bill_no (supplier invoice number)."""
    if not settings.erpnext_url or not settings.erpnext_api_key:
        raise HTTPException(status_code=400, detail="ERPNext credentials not configured")

    client = ERPNextClient(
        url=settings.erpnext_url,
        api_key=settings.erpnext_api_key,
        api_secret=settings.erpnext_api_secret,
    )

    try:
        pi = client.get_purchase_invoice_by_bill_no(bill_no)
    except Exception as e:
        logger.error("review.ground_truth_error", bill_no=bill_no, error=str(e))
        raise HTTPException(status_code=502, detail=f"ERPNext lookup failed: {e}")

    if pi is None:
        raise HTTPException(status_code=404, detail=f"No Purchase Invoice found with bill_no={bill_no}")

    # Reuse the backtest extractor's ground truth function
    from scripts.backtest.extractor import extract_ground_truth
    return extract_ground_truth(pi)
