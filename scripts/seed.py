#!/usr/bin/env python3
"""Seed ChromaDB with ERPNext master data for tenant_a.

Usage:
    # Start ChromaDB first:  docker compose up chromadb -d
    # Then run:
    python scripts/seed.py                          # defaults to seed_erpnext.json
    python scripts/seed.py scripts/seed_erpnext.json   # explicit path
    python scripts/seed.py --api                    # seed via API instead of direct

Two modes:
  Direct (default) — loads model locally, writes to ChromaDB directly.
  API (--api)      — posts to the running FastAPI /api/v1/sync endpoint.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SEED_FILE = Path(__file__).parent / "seed_erpnext.json"
API_BASE = "http://localhost:8080"


def seed_direct(data: dict) -> None:
    """Seed by writing to ChromaDB directly (no API server needed)."""
    from app.config import get_settings
    from app.services.embedding_service import EmbeddingService
    from app.services.vector_service import VectorService

    settings = get_settings()
    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)

    tenant_id = data["tenant_id"]
    erp_system = data["erp_system"]
    now = datetime.now(timezone.utc)

    for entity in ("vendors", "items", "tax_codes", "uoms"):
        records = data.get(entity, [])
        if not records:
            print(f"  {entity}: no records, skipping")
            continue

        print(f"  {entity}: encoding {len(records)} records...")
        count = vector_svc.upsert(
            entity=entity,
            tenant_id=tenant_id,
            erp_system=erp_system,
            records=records,
            synced_at=now,
            embedding_fn=embedding_svc.encode,
        )
        print(f"  {entity}: {count} records upserted")

    print("\nDone! Collections created:")
    for entity in ("vendors", "items", "tax_codes", "uoms"):
        name = vector_svc.collection_name(entity, tenant_id, erp_system)
        print(f"  - {name}")


def seed_via_api(data: dict) -> None:
    """Seed by posting to the /api/v1/sync endpoint."""
    import httpx

    tenant_id = data["tenant_id"]
    erp_system = data["erp_system"]
    now = datetime.now(timezone.utc).isoformat()

    for entity in ("vendors", "items", "tax_codes", "uoms"):
        records = data.get(entity, [])
        if not records:
            print(f"  {entity}: no records, skipping")
            continue

        print(f"  {entity}: syncing {len(records)} records via API...")
        resp = httpx.post(
            f"{API_BASE}/api/v1/sync",
            json={
                "entity": entity,
                "tenant_id": tenant_id,
                "erp_system": erp_system,
                "records": records,
                "synced_at": now,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"  {entity}: {result['records_upserted']} records upserted")

    print("\nDone!")


def main():
    use_api = "--api" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    seed_path = Path(args[0]) if args else SEED_FILE
    if not seed_path.exists():
        print(f"Seed file not found: {seed_path}")
        sys.exit(1)

    with open(seed_path) as f:
        data = json.load(f)

    print(f"Seeding {data['erp_system']} master data for tenant '{data['tenant_id']}'")
    print(f"Mode: {'API' if use_api else 'Direct'}\n")

    if use_api:
        seed_via_api(data)
    else:
        seed_direct(data)


if __name__ == "__main__":
    main()
