"""CLI entry point for ERPNext backtesting.

Usage:
    python -m scripts.backtest.run \
        --url https://site.erpnext.com \
        --api-key <key> --api-secret <secret> \
        --tenant-id tenant_a --erp-system erpnext \
        --invoices-dir ./vllm_outputs/ \
        --seed \
        --limit 50 \
        --output reports/backtest_results
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models.canonical import CanonicalInvoice
from app.services.embedding_service import EmbeddingService
from app.services.mapper import MapperService
from app.services.vector_service import VectorService
from scripts.backtest.erpnext_client import ERPNextClient
from scripts.backtest.evaluator import Evaluator, InvoiceResult
from scripts.backtest.extractor import (
    addresses_to_seed_records,
    companies_to_seed_records,
    cost_centers_to_seed_records,
    extract_ground_truth,
    items_to_seed_records,
    purchase_tax_templates_to_seed_records,
    suppliers_to_seed_records,
    tax_templates_to_seed_records,
    uoms_to_seed_records,
    warehouses_to_seed_records,
)
from scripts.backtest.report import print_summary, save_csv_report, save_json_report


class _MapOptions:
    """Minimal options object matching the MapOptions interface."""

    confidence_threshold: float = 0.88
    return_candidates: bool = True
    dry_run: bool = False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest mapper accuracy against ERPNext data",
    )
    parser.add_argument("--url", default=os.getenv("ERPNEXT_URL"), help="ERPNext site URL")
    parser.add_argument("--api-key", default=os.getenv("ERPNEXT_API_KEY"), help="API key")
    parser.add_argument("--api-secret", default=os.getenv("ERPNEXT_API_SECRET"), help="API secret")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID for collection naming")
    parser.add_argument("--erp-system", default="erpnext", help="ERP system name")
    parser.add_argument("--invoices-dir", default=None, help="Directory of vLLM canonical JSON files")
    parser.add_argument("--seed", action="store_true", help="Seed ChromaDB from ERPNext master data")
    parser.add_argument("--seed-only", action="store_true", help="Seed ChromaDB and exit (no backtest)")
    parser.add_argument("--limit", type=int, default=0, help="Limit master data records fetched")
    parser.add_argument("--output", default="reports/backtest", help="Output path prefix for reports")
    return parser.parse_args()


def _seed_from_erpnext(
    client: ERPNextClient,
    vector_svc: VectorService,
    embedding_svc: EmbeddingService,
    tenant_id: str,
    erp_system: str,
    limit: int,
) -> None:
    """Fetch master data from ERPNext and seed ChromaDB."""
    now = datetime.now(timezone.utc)

    print("Fetching suppliers...")
    suppliers = client.get_suppliers(limit=limit)
    records = suppliers_to_seed_records(suppliers)
    count = vector_svc.upsert("vendors", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} vendors")

    print("Fetching items...")
    items = client.get_items(limit=limit)
    records = items_to_seed_records(items)
    count = vector_svc.upsert("items", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} items")

    print("Fetching tax templates...")
    templates = client.get_tax_templates(limit=limit)
    records = tax_templates_to_seed_records(templates)
    count = vector_svc.upsert("tax_codes", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} tax codes")

    print("Fetching UOMs...")
    uoms = client.get_uoms(limit=limit)
    records = uoms_to_seed_records(uoms)
    count = vector_svc.upsert("uoms", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} UOMs")

    print("Fetching companies...")
    companies = client.get_companies(limit=limit)
    records = companies_to_seed_records(companies)
    count = vector_svc.upsert("companies", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} companies")

    print("Fetching addresses...")
    addresses = client.get_addresses(limit=limit)
    records = addresses_to_seed_records(addresses)
    count = vector_svc.upsert("addresses", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} addresses")

    print("Fetching cost centers...")
    cost_centers = client.get_cost_centers(limit=limit)
    records = cost_centers_to_seed_records(cost_centers)
    count = vector_svc.upsert("cost_centers", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} cost centers")

    print("Fetching warehouses...")
    warehouses = client.get_warehouses(limit=limit)
    records = warehouses_to_seed_records(warehouses)
    count = vector_svc.upsert("warehouses", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} warehouses")

    print("Fetching purchase tax templates...")
    ptemplates = client.get_purchase_taxes_templates(limit=limit)
    records = purchase_tax_templates_to_seed_records(ptemplates)
    count = vector_svc.upsert("tax_templates", tenant_id, erp_system, records, now, embedding_svc.encode)
    print(f"  Seeded {count} purchase tax templates")


def _load_canonical_invoices(invoices_dir: str) -> list[CanonicalInvoice]:
    """Load canonical invoice JSON files from a directory."""
    path = Path(invoices_dir)
    if not path.is_dir():
        print(f"Error: {invoices_dir} is not a directory")
        sys.exit(1)

    invoices = []
    for f in sorted(path.glob("*.json")):
        with open(f) as fp:
            data = json.load(fp)
        invoices.append(CanonicalInvoice(**data))

    if not invoices:
        print(f"No JSON files found in {invoices_dir}")
        sys.exit(1)

    print(f"Loaded {len(invoices)} canonical invoices")
    return invoices


def main() -> None:
    args = _parse_args()

    if not args.url or not args.api_key or not args.api_secret:
        print("Error: ERPNext credentials required (--url, --api-key, --api-secret or env vars)")
        sys.exit(1)

    # Initialize services
    settings = Settings()
    vector_svc = VectorService(settings)
    embedding_svc = EmbeddingService(settings)
    mapper_svc = MapperService(settings)
    erp_client = ERPNextClient(args.url, args.api_key, args.api_secret)
    evaluator = Evaluator()
    options = _MapOptions()

    # Step 1: Optionally seed ChromaDB
    if args.seed or args.seed_only:
        print("\n--- Seeding ChromaDB from ERPNext ---")
        _seed_from_erpnext(
            erp_client, vector_svc, embedding_svc,
            args.tenant_id, args.erp_system, args.limit,
        )
        if args.seed_only:
            print("\nSeeding complete.")
            return

    # Step 2: Load canonical invoices
    if not args.invoices_dir:
        print("Error: --invoices-dir is required for backtest (or use --seed-only)")
        sys.exit(1)

    print("\n--- Loading canonical invoices ---")
    invoices = _load_canonical_invoices(args.invoices_dir)

    # Step 3: Run each invoice through mapper + evaluate
    print("\n--- Running backtest ---")
    invoice_results: list[InvoiceResult] = []

    for inv in invoices:
        # Look up matching Purchase Invoice in ERPNext
        try:
            pi = erp_client.get_purchase_invoice(inv.invoice_number)
            ground_truth = extract_ground_truth(pi)
        except Exception as e:
            print(f"  Skipping {inv.invoice_number}: could not fetch PI ({e})")
            continue

        # Run through mapper
        try:
            response = mapper_svc.map(inv, args.erp_system, args.tenant_id, options)
            response_dict = response.model_dump()
        except Exception as e:
            print(f"  Error mapping {inv.invoice_number}: {e}")
            continue

        # Evaluate
        inv_result = evaluator.evaluate_invoice(
            response_dict, ground_truth, inv.invoice_number,
        )
        invoice_results.append(inv_result)
        status_icon = "+" if inv_result.accuracy >= 0.8 else "-"
        print(f"  [{status_icon}] {inv.invoice_number}: {inv_result.accuracy:.0%}")

    if not invoice_results:
        print("No invoices were successfully evaluated.")
        sys.exit(1)

    # Step 4: Aggregate and report
    batch_result = evaluator.evaluate_batch(invoice_results)
    print_summary(batch_result)

    json_path = save_json_report(batch_result, args.output)
    csv_path = save_csv_report(batch_result, args.output)
    print(f"Reports saved: {json_path}, {csv_path}")


if __name__ == "__main__":
    main()
