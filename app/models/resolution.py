"""Resolution models used internally during the mapping pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ResolutionStrategy(str, Enum):
    """How an FK field was resolved."""

    HARD_KEY = "hard_key"
    FILTERED_SEMANTIC = "filtered_semantic"
    PURE_SEMANTIC = "pure_semantic"
    NOT_FOUND = "not_found"


class VendorStatus(str, Enum):
    """Outcome of the vendor resolution stage."""

    FOUND = "found"
    SUGGEST = "suggest"
    REVIEW = "review"
    NOT_FOUND = "not_found"
    STALE_DATA = "stale_data"


class FKMatch(BaseModel):
    """Result of resolving one foreign-key field."""

    erp_id: Any | None = None
    matched_on: str | None = Field(
        None,
        description="The text or key that produced the match",
    )
    strategy: ResolutionStrategy
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Resolution confidence (computed by this service)",
    )
    candidates: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Top-N candidate matches with scores",
    )


class InvoiceContext(BaseModel):
    """Enrichment context built after vendor resolution (Stage 4).

    Carries forward information that downstream resolvers
    (items, tax, uom) need.
    """

    vendor_known: bool = False
    vendor_erp_id: Any | None = None
    tax_scope: str | None = Field(
        None,
        description="INTRA_REGION, INTER_REGION, or IMPORT — derived from country/region comparison",
    )
    tax_component: str | None = Field(
        None,
        description="ERP-specific tax component filter value (mapped from tax_scope via ERP schema)",
    )
    item_group_filter: str | None = Field(
        None,
        description="Vendor category used to narrow item search",
    )
    confidence_floor: float = Field(
        0.50,
        description="Minimum confidence to accept line-item matches",
    )
    preferred_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Historic vendor→item mappings from approved invoices. "
            "Each dict has: item_erp_id, item_code, hsn_code, description, frequency."
        ),
    )
    verified_tax_id: str | None = Field(
        None,
        description="Human-verified tax ID from feedback loop",
    )


class ResolvedLineItem(BaseModel):
    """Resolution results for a single line item."""

    index: int
    item: FKMatch
    uom: FKMatch
    tax: FKMatch
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Original canonical line-item data for reference",
    )
