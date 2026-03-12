#!/usr/bin/env python3
"""Discover ERPNext data structure for a site.

Dumps a summary of suppliers, items, tax templates, companies,
and purchase invoices to help configure the mapper pipeline.

Usage:
    python scripts/discover_erpnext.py \
        --url http://site.alfarsi:8000 \
        --api-key <key> --api-secret <secret>

    # Limit records fetched per entity:
    python scripts/discover_erpnext.py \
        --url http://site.alfarsi:8000 \
        --api-key <key> --api-secret <secret> \
        --limit 5

    # Save to file:
    python scripts/discover_erpnext.py \
        --url http://site.alfarsi:8000 \
        --api-key <key> --api-secret <secret> \
        --output reports/erpnext_discovery.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.connectors.erpnext import ERPNextClient


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover ERPNext data structure")
    parser.add_argument("--url", default=os.getenv("ERPNEXT_URL"), help="ERP site URL")
    parser.add_argument("--api-key", default=os.getenv("ERPNEXT_API_KEY"), help="API key")
    parser.add_argument("--api-secret", default=os.getenv("ERPNEXT_API_SECRET"), help="API secret")
    parser.add_argument("--limit", type=int, default=5, help="Max records per entity (default: 5)")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    return parser.parse_args()


def _summarise(data: list[dict], label: str) -> None:
    """Print a quick summary of fetched records."""
    print(f"\n{'='*60}")
    print(f"  {label}: {len(data)} records")
    print(f"{'='*60}")
    if not data:
        print("  (none)")
        return
    # Show field names from first record
    print(f"  Fields: {', '.join(data[0].keys())}")
    for i, rec in enumerate(data):
        print(f"\n  [{i+1}] {rec.get('name', '?')}")
        for k, v in rec.items():
            if k in ("doctype", "docstatus", "modified", "creation", "owner",
                      "modified_by", "idx", "naming_series"):
                continue
            # Truncate long values
            val_str = str(v)
            if len(val_str) > 120:
                val_str = val_str[:120] + "..."
            print(f"      {k}: {val_str}")


def main() -> None:
    args = _parse_args()
    if not args.url or not args.api_key or not args.api_secret:
        print("Error: credentials required (--url, --api-key, --api-secret or env vars)")
        sys.exit(1)

    client = ERPNextClient(args.url, args.api_key, args.api_secret)
    limit = args.limit

    print(f"Discovering ERPNext data at: {args.url}")
    print(f"Limit per entity: {limit}")

    result: dict[str, list] = {}

    # Companies
    print("\nFetching companies...")
    companies = client.get_companies(limit=limit)
    result["companies"] = companies
    _summarise(companies, "Companies")

    # Suppliers
    print("\nFetching suppliers...")
    suppliers = client.get_suppliers(limit=limit)
    result["suppliers"] = suppliers
    _summarise(suppliers, "Suppliers")

    # Items
    print("\nFetching items...")
    items = client.get_items(limit=limit)
    result["items"] = items
    _summarise(items, "Items")

    # Item Tax Templates (with taxes child table)
    print("\nFetching item tax templates...")
    tax_templates = client.get_tax_templates(limit=limit)
    result["item_tax_templates"] = tax_templates
    _summarise(tax_templates, "Item Tax Templates")

    # Purchase Taxes and Charges Templates
    print("\nFetching purchase taxes templates...")
    purchase_tax_templates = client.get_purchase_taxes_templates(limit=limit)
    result["purchase_tax_templates"] = purchase_tax_templates
    _summarise(purchase_tax_templates, "Purchase Taxes Templates")

    # UOMs
    print("\nFetching UOMs...")
    uoms = client.get_uoms(limit=limit)
    result["uoms"] = uoms
    _summarise(uoms, "UOMs")

    # Purchase Invoices (first few)
    print("\nFetching purchase invoices...")
    pi_summaries = client.get_purchase_invoices(limit=min(limit, 3))
    invoices = []
    for pi in pi_summaries:
        full = client.get_purchase_invoice(pi["name"])
        invoices.append(full)
    result["purchase_invoices"] = invoices
    _summarise(invoices, "Purchase Invoices")

    # Save to file if requested
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Make JSON serializable (handle dates, etc.)
        def _default(o: object) -> str:
            return str(o)

        out_path.write_text(json.dumps(result, indent=2, default=_default))
        print(f"\nFull discovery saved to: {out_path}")

    # Print key insights
    print(f"\n{'='*60}")
    print("  KEY INSIGHTS")
    print(f"{'='*60}")

    if companies:
        c = companies[0]
        print(f"  Company: {c.get('company_name', '?')}")
        print(f"  Country: {c.get('country', '?')}")
        print(f"  Currency: {c.get('default_currency', '?')}")

    if tax_templates:
        print(f"\n  Tax templates found:")
        for t in tax_templates:
            taxes = t.get("taxes", [])
            tax_info = ""
            if taxes:
                first = taxes[0]
                tax_info = f" → {first.get('tax_type', '?')} @ {first.get('tax_rate', '?')}%"
            print(f"    - {t.get('name', '?')}{tax_info}")

    if purchase_tax_templates:
        print(f"\n  Purchase tax charge templates:")
        for pt in purchase_tax_templates:
            print(f"    - {pt.get('name', '?')} (title: {pt.get('title', '?')})")

    supplier_countries = set()
    for s in suppliers:
        if s.get("country"):
            supplier_countries.add(s["country"])
    if supplier_countries:
        print(f"\n  Supplier countries: {', '.join(sorted(supplier_countries))}")

    has_tax_id = sum(1 for s in suppliers if s.get("tax_id") or s.get("gstin"))
    print(f"  Suppliers with tax_id: {has_tax_id}/{len(suppliers)}")


if __name__ == "__main__":
    main()
