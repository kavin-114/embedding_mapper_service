"""Pull-based sync — connects to client's ERP, fetches master data, seeds ChromaDB."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.services.embedding_service import EmbeddingService
from app.services.vector_service import VectorService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["pull-sync"])

ALL_ENTITIES = (
    "vendors", "items", "tax_codes", "uoms",
    "companies", "addresses", "cost_centers", "warehouses", "tax_templates",
)


class PullSyncRequest(BaseModel):
    """Credentials and config for pulling data from a client's ERP."""

    erp_system: str = Field(..., description="ERP system identifier (e.g. 'erpnext')")
    tenant_id: str = Field(..., description="Tenant identifier for collection naming")

    # Connection credentials
    erp_url: str = Field(..., description="Base URL of the ERP site (e.g. https://site.erpnext.com)")
    api_key: str = Field(..., description="API key for authentication")
    api_secret: str = Field(..., description="API secret for authentication")

    # Optional filters
    entities: list[str] | None = Field(
        None,
        description="Specific entities to sync. If null, syncs all 9 entity types.",
    )
    limit: int = Field(0, description="Limit records per entity (0 = all)")


class EntitySyncResult(BaseModel):
    entity: str
    records_fetched: int
    records_upserted: int
    error: str | None = None


class PullSyncResponse(BaseModel):
    tenant_id: str
    erp_system: str
    entities: list[EntitySyncResult]
    total_upserted: int
    synced_at: str


def _get_connector(erp_system: str, erp_url: str, api_key: str, api_secret: str) -> Any:
    """Get the appropriate ERP connector based on erp_system."""
    if erp_system == "erpnext":
        from app.services.connectors.erpnext import ERPNextClient
        return ERPNextClient(erp_url, api_key, api_secret)
    raise HTTPException(
        status_code=400,
        detail=f"Pull sync not supported for erp_system '{erp_system}'. Supported: erpnext",
    )


@router.post("/pull-sync", response_model=PullSyncResponse)
async def pull_sync(
    body: PullSyncRequest,
    settings: Settings = Depends(get_settings),
) -> PullSyncResponse:
    """Pull master data from the client's ERP and seed ChromaDB.

    Connects to the ERP using provided credentials, fetches all master data
    entities (or a subset), converts them to seed records, and upserts into
    the appropriate ChromaDB collections.
    """
    from app.services.connectors.erpnext_extractors import ENTITY_SYNC_MAP

    client = _get_connector(body.erp_system, body.erp_url, body.api_key, body.api_secret)

    entities_to_sync = body.entities or list(ALL_ENTITIES)
    # Validate requested entities
    invalid = [e for e in entities_to_sync if e not in ALL_ENTITIES]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entities: {invalid}. Valid: {list(ALL_ENTITIES)}",
        )

    embedding_svc = EmbeddingService(settings)
    vector_svc = VectorService(settings)
    now = datetime.now(timezone.utc)

    results: list[EntitySyncResult] = []
    total = 0

    logger.info(
        "pull_sync.start",
        tenant_id=body.tenant_id,
        erp_system=body.erp_system,
        erp_url=body.erp_url,
        entities=entities_to_sync,
    )

    for entity in entities_to_sync:
        sync_entry = ENTITY_SYNC_MAP.get(entity)
        if not sync_entry:
            results.append(EntitySyncResult(
                entity=entity, records_fetched=0, records_upserted=0,
                error=f"No sync mapping for entity '{entity}'",
            ))
            continue

        method_name, converter = sync_entry

        try:
            # Fetch from ERP
            fetcher = getattr(client, method_name)
            raw_docs = fetcher(limit=body.limit)

            # Convert to seed records
            records = converter(raw_docs)

            # Upsert into ChromaDB
            count = vector_svc.upsert(
                entity=entity,
                tenant_id=body.tenant_id,
                erp_system=body.erp_system,
                records=records,
                synced_at=now,
                embedding_fn=embedding_svc.encode,
            )

            results.append(EntitySyncResult(
                entity=entity,
                records_fetched=len(raw_docs),
                records_upserted=count,
            ))
            total += count

            logger.info(
                "pull_sync.entity_done",
                entity=entity,
                fetched=len(raw_docs),
                upserted=count,
            )

        except Exception as e:
            logger.error(
                "pull_sync.entity_error",
                entity=entity,
                error=str(e),
            )
            results.append(EntitySyncResult(
                entity=entity, records_fetched=0, records_upserted=0,
                error=str(e),
            ))

    logger.info(
        "pull_sync.complete",
        tenant_id=body.tenant_id,
        total_upserted=total,
    )

    return PullSyncResponse(
        tenant_id=body.tenant_id,
        erp_system=body.erp_system,
        entities=results,
        total_upserted=total,
        synced_at=now.isoformat(),
    )
