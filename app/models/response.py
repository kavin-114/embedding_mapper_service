"""API response models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MappingStatus(str, Enum):
    """Overall mapping outcome."""

    AUTO_MAP = "auto_map"
    SUGGEST = "suggest"
    REVIEW = "review"
    NO_MATCH = "no_match"


class MappingDetail(BaseModel):
    """Resolution detail for a single mapped field."""

    status: MappingStatus
    erp_id: Any | None = None
    confidence: float = 0.0
    strategy: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class MapResponse(BaseModel):
    """Top-level response from POST /api/v1/map."""

    status: str = Field(
        ...,
        description="Overall result: success | partial | failed",
    )
    erp_payload: dict[str, Any] | None = Field(
        None,
        description="Fully transformed ERP-specific payload (null on dry_run)",
    )
    mappings: dict[str, MappingDetail] = Field(
        default_factory=dict,
        description="Per-field resolution details keyed by canonical field name",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Canonical field names that could not be resolved",
    )
    review_required: list[str] = Field(
        default_factory=list,
        description="Fields flagged for human review",
    )


class SyncResponse(BaseModel):
    """Response from POST /api/v1/sync."""

    entity: str
    tenant_id: str
    erp_system: str
    records_upserted: int
