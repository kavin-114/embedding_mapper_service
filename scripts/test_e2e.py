"""End-to-end test script for the extractor → mapper pipeline.

Usage:
    python scripts/test_e2e.py --dir ../invoice-extractor/output/ --erp erpnext --tenant tenant_a
    python scripts/test_e2e.py --file invoice.json --erp erpnext --tenant tenant_a

Reads extractor JSON files from a directory (or single file), adapts each to
canonical format, runs through the mapping pipeline, and reports results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.models.extractor import ExtractorInvoice
from app.routers.map import MapOptions
from app.services.extractor_adapter import adapt
from app.services.mapper import MapperService


def process_file(
    path: Path,
    mapper: MapperService,
    erp_system: str,
    tenant_id: str,
    options: MapOptions,
) -> dict:
    """Process a single extractor JSON file and return a summary."""
    raw = json.loads(path.read_text())
    extractor_invoice = ExtractorInvoice(**raw)
    canonical = adapt(extractor_invoice)

    try:
        result = mapper.map(
            invoice=canonical,
            erp_system=erp_system,
            tenant_id=tenant_id,
            options=options,
        )
        return {
            "file": path.name,
            "status": result.status,
            "invoice_number": canonical.invoice_number,
            "vendor": canonical.vendor_name.value,
            "line_items": len(canonical.line_items),
            "unresolved": result.unresolved,
            "review_required": result.review_required,
        }
    except Exception as e:
        return {
            "file": path.name,
            "status": "error",
            "error": str(e),
        }


def main():
    parser = argparse.ArgumentParser(description="E2E extractor→mapper test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", type=Path, help="Directory of extractor JSON files")
    group.add_argument("--file", type=Path, help="Single extractor JSON file")
    parser.add_argument("--erp", default="erpnext", help="ERP system (default: erpnext)")
    parser.add_argument("--tenant", default="tenant_a", help="Tenant ID (default: tenant_a)")
    parser.add_argument("--dry-run", action="store_true", help="Skip transform stage")
    parser.add_argument("--output", type=Path, help="Write results JSON to file")
    args = parser.parse_args()

    settings = get_settings()
    mapper = MapperService(settings)
    options = MapOptions(dry_run=args.dry_run)

    if args.file:
        files = [args.file]
    else:
        files = sorted(args.dir.glob("*.json"))
        if not files:
            print(f"No JSON files found in {args.dir}")
            sys.exit(1)

    results = []
    for path in files:
        print(f"Processing {path.name}...")
        result = process_file(path, mapper, args.erp, args.tenant, options)
        results.append(result)

        status = result.get("status", "?")
        if status == "error":
            print(f"  ERROR: {result.get('error')}")
        else:
            print(f"  Status: {status} | Invoice: {result.get('invoice_number')} | "
                  f"Vendor: {result.get('vendor')} | Lines: {result.get('line_items')}")
            if result.get("unresolved"):
                print(f"  Unresolved: {result['unresolved']}")
            if result.get("review_required"):
                print(f"  Review: {result['review_required']}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Total: {len(results)}")
    for s in ("success", "partial", "failed", "error"):
        count = sum(1 for r in results if r.get("status") == s)
        if count:
            print(f"  {s}: {count}")

    if args.output:
        args.output.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
