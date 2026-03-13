"""Pipeline metrics — accumulated per-request and serialized as structured log."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldDetail:
    """Per-field resolution detail for metrics."""

    field_name: str
    strategy: str | None = None
    confidence: float = 0.0
    status: str | None = None
    erp_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "status": self.status,
            "erp_id": self.erp_id,
        }


@dataclass
class PipelineMetrics:
    """Accumulated metrics for a single mapping request."""

    # Stage timings (ms)
    schema_load_ms: float = 0.0
    vendor_resolution_ms: float = 0.0
    unknown_vendor_ms: float = 0.0
    context_enrichment_ms: float = 0.0
    line_items_ms: float = 0.0
    transform_ms: float = 0.0
    total_ms: float = 0.0

    # Vendor resolution
    vendor_strategy: str | None = None
    vendor_confidence: float = 0.0
    vendor_status: str | None = None
    vendor_erp_id: str | None = None

    # Line item counts
    line_item_count: int = 0
    auto_map_count: int = 0
    suggest_count: int = 0
    review_count: int = 0
    no_match_count: int = 0

    # Per-field details
    field_details: list[FieldDetail] = field(default_factory=list)

    def add_field(
        self,
        field_name: str,
        strategy: str | None = None,
        confidence: float = 0.0,
        status: str | None = None,
        erp_id: str | None = None,
    ) -> None:
        self.field_details.append(FieldDetail(
            field_name=field_name,
            strategy=strategy,
            confidence=confidence,
            status=status,
            erp_id=erp_id,
        ))

    def count_status(self, status: str) -> None:
        if status == "auto_map":
            self.auto_map_count += 1
        elif status == "suggest":
            self.suggest_count += 1
        elif status == "review":
            self.review_count += 1
        elif status == "no_match":
            self.no_match_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "timings": {
                "schema_load_ms": round(self.schema_load_ms, 2),
                "vendor_resolution_ms": round(self.vendor_resolution_ms, 2),
                "unknown_vendor_ms": round(self.unknown_vendor_ms, 2),
                "context_enrichment_ms": round(self.context_enrichment_ms, 2),
                "line_items_ms": round(self.line_items_ms, 2),
                "transform_ms": round(self.transform_ms, 2),
                "total_ms": round(self.total_ms, 2),
            },
            "vendor": {
                "strategy": self.vendor_strategy,
                "confidence": self.vendor_confidence,
                "status": self.vendor_status,
                "erp_id": self.vendor_erp_id,
            },
            "line_items": {
                "count": self.line_item_count,
                "auto_map": self.auto_map_count,
                "suggest": self.suggest_count,
                "review": self.review_count,
                "no_match": self.no_match_count,
            },
            "field_details": [f.to_dict() for f in self.field_details],
        }
