#!/usr/bin/env python3
"""Pull master data from ERPNext and seed ChromaDB.

Usage:
    # Pull ALL master data (full sync):
    python scripts/pull_sync.py \
        --url https://site.erpnext.com \
        --api-key <key> --api-secret <secret> \
        --tenant-id alfarsi --erp-system erpnext

    # Pull only masters referenced by Purchase Invoices (recommended for backtest):
    python scripts/pull_sync.py \
        --url https://site.erpnext.com \
        --api-key <key> --api-secret <secret> \
        --tenant-id alfarsi --erp-system erpnext \
        --pi-referenced

    # Pull specific entities only:
    python scripts/pull_sync.py \
        --url https://site.erpnext.com \
        --api-key <key> --api-secret <secret> \
        --tenant-id alfarsi --erp-system erpnext \
        --entities vendors items tax_codes

Environment variables (alternative to CLI args):
    ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add project root to path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

# Load .env before argparse reads os.getenv defaults
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(_PROJECT_ROOT) / ".env")

ALL_ENTITIES = (
    "vendors", "items", "tax_codes", "uoms",
    "companies", "addresses", "cost_centers", "warehouses", "tax_templates",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull ERP master data and seed ChromaDB collections",
    )
    parser.add_argument("--url", default=os.getenv("ERPNEXT_URL"), help="ERP site URL")
    parser.add_argument("--api-key", default=os.getenv("ERPNEXT_API_KEY"), help="API key")
    parser.add_argument("--api-secret", default=os.getenv("ERPNEXT_API_SECRET"), help="API secret")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID for collection naming")
    parser.add_argument("--erp-system", default="erpnext", help="ERP system name")
    parser.add_argument(
        "--entities", nargs="+", default=None,
        help=f"Entities to sync (default: all). Choices: {', '.join(ALL_ENTITIES)}",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit records per entity (0 = all)")
    parser.add_argument(
        "--pi-referenced", action="store_true",
        help="Only pull masters referenced by Purchase Invoices (saves PIs as ground truth)",
    )
    parser.add_argument(
        "--with-attachments", action="store_true",
        help="Only include PIs that have file attachments (use with --pi-referenced)",
    )
    parser.add_argument(
        "--pi-filters", type=str, default=None,
        help='JSON filters for PI fetch, e.g. \'{"docstatus": 1}\' (default: submitted only)',
    )
    parser.add_argument(
        "--pi-limit", type=int, default=0,
        help="Limit number of PIs to fetch (0 = all, default: 0)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="data/ground_truth",
        help="Directory to save PI ground truth (default: data/ground_truth)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help="Max concurrent API requests (default: 10)",
    )
    return parser.parse_args()


def _extract_pi_references(invoices: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Extract unique master record names referenced across all Purchase Invoices."""
    refs: dict[str, set[str]] = {
        "vendors": set(),       # Supplier doctype
        "items": set(),         # Item doctype
        "tax_codes": set(),     # Item Tax Template doctype
        "uoms": set(),          # UOM doctype
        "companies": set(),     # Company doctype
        "addresses": set(),     # Address doctype
        "cost_centers": set(),  # Cost Center doctype
        "warehouses": set(),    # Warehouse doctype
        "tax_templates": set(), # Purchase Taxes and Charges Template
    }

    for pi in invoices:
        # Header-level references
        if pi.get("supplier"):
            refs["vendors"].add(pi["supplier"])
        if pi.get("company"):
            refs["companies"].add(pi["company"])
        if pi.get("supplier_address"):
            refs["addresses"].add(pi["supplier_address"])
        if pi.get("shipping_address"):
            refs["addresses"].add(pi["shipping_address"])
        if pi.get("billing_address"):
            refs["addresses"].add(pi["billing_address"])
        if pi.get("cost_center"):
            refs["cost_centers"].add(pi["cost_center"])
        if pi.get("taxes_and_charges"):
            refs["tax_templates"].add(pi["taxes_and_charges"])

        # Line-item references
        for item in pi.get("items", []):
            if item.get("item_code"):
                refs["items"].add(item["item_code"])
            if item.get("uom"):
                refs["uoms"].add(item["uom"])
            if item.get("item_tax_template"):
                refs["tax_codes"].add(item["item_tax_template"])
            if item.get("warehouse"):
                refs["warehouses"].add(item["warehouse"])
            if item.get("cost_center"):
                refs["cost_centers"].add(item["cost_center"])

    return refs


def _save_ground_truth(invoices: list[dict[str, Any]], output_dir: str) -> None:
    """Save Purchase Invoices as ground truth JSON files for backtesting."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Save individual PI files
    for pi in invoices:
        name = pi.get("name", "unknown").replace("/", "_")
        filepath = out / f"{name}.json"
        filepath.write_text(json.dumps(pi, indent=2, default=str))

    # Save summary manifest
    manifest = {
        "count": len(invoices),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "invoices": [
            {
                "name": pi.get("name"),
                "supplier": pi.get("supplier"),
                "grand_total": pi.get("grand_total"),
                "items_count": len(pi.get("items", [])),
            }
            for pi in invoices
        ],
    }
    (out / "_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(f"  Ground truth saved to {out}/ ({len(invoices)} invoices)")


def _run_pi_referenced(args: argparse.Namespace) -> None:
    """Pull only masters referenced by Purchase Invoices."""
    from app.config import Settings
    from app.services.connectors.erpnext import ERPNextClient
    from app.services.connectors.erpnext_extractors import ENTITY_SYNC_MAP
    from app.services.embedding_service import EmbeddingService
    from app.services.vector_service import VectorService

    settings = Settings()
    client = ERPNextClient(
        args.url, args.api_key, args.api_secret,
        max_workers=args.concurrency,
    )
    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)
    now = datetime.now(timezone.utc)

    # Parse PI filters
    pi_filters = json.loads(args.pi_filters) if args.pi_filters else {"docstatus": 1}

    print(f"PI-referenced pull: {args.erp_system} → tenant '{args.tenant_id}'")
    print(f"ERP URL: {args.url}")
    print(f"Concurrency: {args.concurrency}")
    print(f"PI filters: {pi_filters}")
    print(f"With attachments only: {args.with_attachments}")
    print(f"PI limit: {args.pi_limit or 'all'}\n")

    # Step 1: Fetch Purchase Invoices
    pi_names: list[str] | None = None
    if args.with_attachments:
        print("Step 1a: Finding PIs with file attachments...")
        pi_names = client.get_pi_names_with_attachments()
        print(f"  Found {len(pi_names)} PIs with attachments")
        if args.pi_limit and len(pi_names) > args.pi_limit:
            pi_names = pi_names[:args.pi_limit]
            print(f"  Limited to {args.pi_limit}")

    print("Step 1: Fetching Purchase Invoices (full docs)...")
    invoices = client.get_purchase_invoices_full(
        filters=pi_filters,
        limit=args.pi_limit,
        names=pi_names,
    )
    print(f"  Fetched {len(invoices)} Purchase Invoices\n")

    if not invoices:
        print("No invoices found. Nothing to do.")
        return

    # Step 2: Extract unique references
    print("Step 2: Extracting referenced masters...")
    refs = _extract_pi_references(invoices)
    for entity, names in refs.items():
        print(f"  {entity}: {len(names)} unique")
    total_refs = sum(len(v) for v in refs.values())
    print(f"  Total unique masters: {total_refs}\n")

    # Step 3: Save ground truth
    print("Step 3: Saving ground truth...")
    _save_ground_truth(invoices, args.output_dir)
    print()

    # Step 4: Fetch and seed each entity (only referenced records)
    print("Step 4: Fetching and seeding referenced masters...")

    # Map entity → client method that accepts names=
    entity_fetcher_map: dict[str, str] = {
        "vendors": "get_suppliers",
        "items": "get_items",
        "tax_codes": "get_tax_templates",
        "uoms": "get_uoms",
        "companies": "get_companies",
        "addresses": "get_addresses",
        "cost_centers": "get_cost_centers",
        "warehouses": "get_warehouses",
        "tax_templates": "get_purchase_taxes_templates",
    }

    total_upserted = 0
    for entity, names_set in refs.items():
        names_list = sorted(names_set)
        if not names_list:
            print(f"  [{entity}] No references, skipping")
            continue

        method_name = entity_fetcher_map[entity]
        _, converter = ENTITY_SYNC_MAP[entity]
        fetcher = getattr(client, method_name)

        print(f"  [{entity}] Fetching {len(names_list)} records...")
        try:
            raw_docs = fetcher(names=names_list)
        except Exception as e:
            print(f"  [{entity}] ERROR: {e}")
            continue

        records = converter(raw_docs)
        print(f"  [{entity}] Fetched {len(raw_docs)} → {len(records)} seed records")

        count = vector_svc.upsert(
            entity=entity,
            tenant_id=args.tenant_id,
            erp_system=args.erp_system,
            records=records,
            synced_at=now,
            embedding_fn=embedding_svc.encode,
        )
        print(f"  [{entity}] Upserted {count} records\n")
        total_upserted += count

    print(f"Done! Total records upserted: {total_upserted}")
    print(f"Ground truth: {args.output_dir}/")
    print(f"\nCollections:")
    for entity in refs:
        if refs[entity]:
            name = vector_svc.collection_name(entity, args.tenant_id, args.erp_system)
            print(f"  - {name} ({len(refs[entity])} records)")


def _run_full_sync(args: argparse.Namespace) -> None:
    """Pull all master data (original behavior)."""
    from app.config import Settings
    from app.services.connectors.erpnext import ERPNextClient
    from app.services.connectors.erpnext_extractors import ENTITY_SYNC_MAP
    from app.services.embedding_service import EmbeddingService
    from app.services.vector_service import VectorService

    entities = args.entities or list(ALL_ENTITIES)
    invalid = [e for e in entities if e not in ALL_ENTITIES]
    if invalid:
        print(f"Error: unknown entities: {invalid}")
        print(f"Valid: {', '.join(ALL_ENTITIES)}")
        sys.exit(1)

    settings = Settings()
    client = ERPNextClient(
        args.url, args.api_key, args.api_secret,
        max_workers=args.concurrency,
    )
    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)
    now = datetime.now(timezone.utc)

    print(f"Pull sync: {args.erp_system} → tenant '{args.tenant_id}'")
    print(f"ERP URL: {args.url}")
    print(f"Concurrency: {args.concurrency}")
    print(f"Entities: {', '.join(entities)}\n")

    total = 0
    for entity in entities:
        method_name, converter = ENTITY_SYNC_MAP[entity]
        fetcher = getattr(client, method_name)

        print(f"  [{entity}] Fetching from ERP...")
        try:
            raw_docs = fetcher(limit=args.limit)
        except Exception as e:
            print(f"  [{entity}] ERROR fetching: {e}")
            continue

        records = converter(raw_docs)
        print(f"  [{entity}] Fetched {len(raw_docs)} docs → {len(records)} records")

        count = vector_svc.upsert(
            entity=entity,
            tenant_id=args.tenant_id,
            erp_system=args.erp_system,
            records=records,
            synced_at=now,
            embedding_fn=embedding_svc.encode,
        )
        print(f"  [{entity}] Upserted {count} records\n")
        total += count

    print(f"Done! Total records upserted: {total}")
    print(f"\nCollections:")
    for entity in entities:
        name = vector_svc.collection_name(entity, args.tenant_id, args.erp_system)
        print(f"  - {name}")


def main() -> None:
    args = _parse_args()

    if not args.url or not args.api_key or not args.api_secret:
        print("Error: ERP credentials required (--url, --api-key, --api-secret or env vars)")
        sys.exit(1)

    if args.pi_referenced:
        _run_pi_referenced(args)
    else:
        _run_full_sync(args)


if __name__ == "__main__":
    main()
