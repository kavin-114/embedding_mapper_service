"""FK resolver — four-strategy resolution engine and vendor unknown handler."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from app.models.canonical import ScoredField, CanonicalInvoice
from app.models.resolution import (
    FKMatch,
    InvoiceContext,
    ResolutionStrategy,
    VendorStatus,
)
from app.services.context_builder import ContextBuilder

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.embedding_service import EmbeddingService
    from app.services.vector_service import VectorService


class Resolver:
    """Resolves foreign-key fields using a four-strategy cascade.

    Strategies (selected by vLLM confidence on the source field):
        1. HARD_KEY           confidence >= 0.90 → exact metadata match
        2. FILTERED_SEMANTIC  confidence >= 0.70 → semantic + metadata filters
        3. PURE_SEMANTIC      confidence >= 0.50 → semantic only
        4. NOT_FOUND          confidence <  0.50 → skip, pure semantic fallback
    """

    def __init__(
        self,
        settings: "Settings",
        embedding_svc: "EmbeddingService",
        vector_svc: "VectorService",
    ) -> None:
        self._settings = settings
        self._embedding = embedding_svc
        self._vector = vector_svc

    # ── vendor resolution (Stage 2 + 3) ─────────────────────────────

    def resolve_vendor(
        self,
        invoice: CanonicalInvoice,
        tenant_id: str,
        erp_system: str,
    ) -> tuple[FKMatch, VendorStatus]:
        """Resolve the vendor FK field.

        Stage 2 flow:
          Step 1: If gstin confidence >= 0.90 → hard match on gstin.
          Step 2: Else → semantic search on vendor_name with state_code boost/penalty.
          Step 3: Evaluate score → FOUND / SUGGEST / NOT_FOUND.
        """
        tax_id_field = invoice.vendor_tax_id
        threshold = self._settings.hard_key_threshold

        # Step 1 — hard match on tax_id if high confidence
        if tax_id_field and tax_id_field.confidence >= threshold:
            meta = self._vector.hard_match(
                entity="vendors",
                tenant_id=tenant_id,
                erp_system=erp_system,
                where={"tax_id": str(tax_id_field.value)},
            )
            if meta is not None:
                return (
                    FKMatch(
                        erp_id=meta["erp_id"],
                        matched_on=f"tax_id={tax_id_field.value}",
                        strategy=ResolutionStrategy.HARD_KEY,
                        confidence=1.0,
                        candidates=[],
                    ),
                    VendorStatus.FOUND,
                )
            # hard key miss — fall through to semantic

        # Step 2 — semantic search on vendor_name
        vendor_text = str(invoice.vendor_name.value)
        query_vec = self._embedding.encode([vendor_text])[0]

        results = self._vector.semantic_search(
            entity="vendors",
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=3,
        )

        if not results:
            return (
                FKMatch(strategy=ResolutionStrategy.NOT_FOUND),
                VendorStatus.NOT_FOUND,
            )

        top = results[0]
        top_score = top["score"]

        # Region-code boost / penalty: compare vendor region with company
        vendor_region = str(top["metadata"].get("region_code", ""))
        company_region = self._settings.company_region_code
        if vendor_region and company_region:
            if vendor_region == company_region:
                top_score = min(1.0, top_score + 0.08)
            else:
                top_score = max(0.0, top_score - 0.15)

        candidates = [
            {"erp_id": r["erp_id"], "score": r["score"], "metadata": r["metadata"]}
            for r in results
        ]

        # Step 3 — evaluate score
        if top_score >= self._settings.auto_map_threshold:
            return (
                FKMatch(
                    erp_id=top["erp_id"],
                    matched_on=vendor_text,
                    strategy=ResolutionStrategy.PURE_SEMANTIC,
                    confidence=top_score,
                    candidates=candidates,
                ),
                VendorStatus.FOUND,
            )
        elif top_score >= self._settings.review_threshold:
            status = (
                VendorStatus.SUGGEST
                if top_score >= self._settings.suggest_threshold
                else VendorStatus.REVIEW
            )
            return (
                FKMatch(
                    erp_id=top["erp_id"],
                    matched_on=vendor_text,
                    strategy=ResolutionStrategy.PURE_SEMANTIC,
                    confidence=top_score,
                    candidates=candidates,
                ),
                status,
            )
        else:
            return (
                FKMatch(
                    strategy=ResolutionStrategy.NOT_FOUND,
                    confidence=top_score,
                    candidates=candidates,
                ),
                VendorStatus.NOT_FOUND,
            )

    def handle_unknown_vendor(
        self,
        invoice: CanonicalInvoice,
        tenant_id: str,
        erp_system: str,
    ) -> tuple[FKMatch, VendorStatus, InvoiceContext]:
        """Stage 3 — Unknown vendor handler.

        1. Check sync freshness — if > sync_stale_hours → flag STALE_DATA.
        2. Try partial match — any result above 0.35 → SUGGEST top 3.
        3. Declare unknown → VENDOR_NOT_FOUND, action CREATE_VENDOR.
        4. Build fallback InvoiceContext.
        """
        flags: list[str] = []

        # 1. Sync freshness check
        sync_time = self._vector.get_sync_time("vendors", tenant_id, erp_system)
        stale = False
        if sync_time is None:
            stale = True
            flags.append("STALE_DATA")
            flags.append("TRIGGER_RESYNC")
        else:
            age_hours = (
                datetime.now(timezone.utc) - sync_time.replace(tzinfo=timezone.utc)
            ).total_seconds() / 3600
            if age_hours > self._settings.sync_stale_hours:
                stale = True
                flags.append("STALE_DATA")
                flags.append("TRIGGER_RESYNC")

        # 2. Try partial match
        vendor_text = str(invoice.vendor_name.value)
        query_vec = self._embedding.encode([vendor_text])[0]
        results = self._vector.semantic_search(
            entity="vendors",
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=3,
        )

        candidates = [
            {"erp_id": r["erp_id"], "score": r["score"], "metadata": r["metadata"]}
            for r in results
        ]

        status: VendorStatus
        match: FKMatch

        if results and results[0]["score"] >= 0.35:
            flags.append("POSSIBLE_MATCH")
            status = VendorStatus.SUGGEST
            match = FKMatch(
                erp_id=results[0]["erp_id"],
                matched_on=vendor_text,
                strategy=ResolutionStrategy.PURE_SEMANTIC,
                confidence=results[0]["score"],
                candidates=candidates,
            )
        else:
            flags.append("VENDOR_NOT_FOUND")
            flags.append("CREATE_VENDOR")
            status = VendorStatus.NOT_FOUND
            match = FKMatch(
                strategy=ResolutionStrategy.NOT_FOUND,
                confidence=0.0,
                candidates=candidates,
            )

        # 4. Build fallback context
        context = InvoiceContext(
            vendor_known=False,
            vendor_erp_id=None,
            tax_scope=None,
            tax_component=None,
            item_group_filter=None,
            confidence_floor=0.50,
        )

        # Attach flags to the match
        match.candidates = candidates
        # Store flags in the first candidate entry for transport
        if candidates:
            candidates[0]["flags"] = flags
        elif flags:
            match.candidates = [{"flags": flags}]

        return match, status, context

    # ── line-item resolvers (Stage 5) ────────────────────────────────

    def resolve_item(
        self,
        line_item_fields: dict[str, ScoredField | None],
        context: InvoiceContext,
        tenant_id: str,
        erp_system: str,
    ) -> FKMatch:
        """Resolve an item FK.

        1. If item_code confidence >= 0.90 → hard match on item_code.
        2. Build filters from vLLM confidence.
        3. Filtered or pure semantic search.
        """
        threshold = self._settings.hard_key_threshold
        filter_thresh = self._settings.filter_threshold

        # Step 1 — hard match on item_code
        item_code = line_item_fields.get("item_code")
        if item_code and item_code.confidence >= threshold:
            meta = self._vector.hard_match(
                entity="items",
                tenant_id=tenant_id,
                erp_system=erp_system,
                where={"item_code": str(item_code.value)},
            )
            if meta is not None:
                return FKMatch(
                    erp_id=meta["erp_id"],
                    matched_on=f"item_code={item_code.value}",
                    strategy=ResolutionStrategy.HARD_KEY,
                    confidence=1.0,
                )

        # Step 2 — build filters
        where_filters: dict[str, Any] = {}
        hsn = line_item_fields.get("hsn_code")
        if hsn and hsn.confidence >= filter_thresh:
            where_filters["hsn_code"] = str(hsn.value)

        uom = line_item_fields.get("uom")
        if uom and uom.confidence >= filter_thresh:
            where_filters["uom"] = str(uom.value)

        if context.item_group_filter:
            where_filters["item_group"] = context.item_group_filter

        # Step 3 — semantic search
        desc = line_item_fields.get("description")
        search_text = str(desc.value) if desc else ""
        query_vec = self._embedding.encode([search_text])[0]

        strategy = (
            ResolutionStrategy.FILTERED_SEMANTIC
            if where_filters
            else ResolutionStrategy.PURE_SEMANTIC
        )

        results = self._vector.semantic_search(
            entity="items",
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=3,
            where=where_filters if where_filters else None,
        )

        if not results:
            return FKMatch(strategy=ResolutionStrategy.NOT_FOUND)

        # Step 4 — boost from vendor history (preferred_items)
        if context.preferred_items:
            preferred_ids = {
                p["item_erp_id"] for p in context.preferred_items if p.get("item_erp_id")
            }
            for r in results:
                if r["erp_id"] in preferred_ids:
                    r["score"] = min(1.0, r["score"] + 0.10)
            # Re-sort after boosting
            results.sort(key=lambda r: r["score"], reverse=True)

        top = results[0]
        candidates = [
            {"erp_id": r["erp_id"], "score": r["score"], "metadata": r["metadata"]}
            for r in results
        ]

        return FKMatch(
            erp_id=top["erp_id"],
            matched_on=search_text,
            strategy=strategy,
            confidence=top["score"],
            candidates=candidates,
        )

    def resolve_uom(
        self,
        uom_field: ScoredField,
        tenant_id: str,
        erp_system: str,
    ) -> FKMatch:
        """Resolve a UOM FK.

        Always tries hard match first (exact uom_code).
        Falls back to semantic search if not found.
        """
        uom_value = str(uom_field.value)

        # Always try hard match first
        meta = self._vector.hard_match(
            entity="uoms",
            tenant_id=tenant_id,
            erp_system=erp_system,
            where={"uom_code": uom_value},
        )
        if meta is not None:
            return FKMatch(
                erp_id=meta["erp_id"],
                matched_on=f"uom_code={uom_value}",
                strategy=ResolutionStrategy.HARD_KEY,
                confidence=1.0,
            )

        # Fallback — semantic search
        query_vec = self._embedding.encode([uom_value])[0]
        results = self._vector.semantic_search(
            entity="uoms",
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=1,
        )

        if not results:
            return FKMatch(strategy=ResolutionStrategy.NOT_FOUND)

        top = results[0]
        return FKMatch(
            erp_id=top["erp_id"],
            matched_on=uom_value,
            strategy=ResolutionStrategy.PURE_SEMANTIC,
            confidence=top["score"],
            candidates=[
                {"erp_id": r["erp_id"], "score": r["score"]}
                for r in results
            ],
        )

    def resolve_tax(
        self,
        tax_rate_field: ScoredField,
        context: InvoiceContext,
        tenant_id: str,
        erp_system: str,
    ) -> FKMatch:
        """Resolve a tax code FK.

        1. If rate confidence >= 0.90 AND component known → hard match.
        2. Else → filtered semantic with rate filter.
        """
        threshold = self._settings.hard_key_threshold
        rate_value = str(tax_rate_field.value)

        # Step 1 — hard match if rate high-confidence AND component known
        if tax_rate_field.confidence >= threshold and context.tax_component:
            where: dict[str, Any] = {
                "$and": [
                    {"rate": rate_value},
                    {"component": context.tax_component},
                ]
            }
            meta = self._vector.hard_match(
                entity="tax_codes",
                tenant_id=tenant_id,
                erp_system=erp_system,
                where=where,
            )
            if meta is not None:
                return FKMatch(
                    erp_id=meta["erp_id"],
                    matched_on=f"rate={rate_value},component={context.tax_component}",
                    strategy=ResolutionStrategy.HARD_KEY,
                    confidence=1.0,
                )

        # Step 2 — filtered semantic
        search_text = f"{rate_value}% tax"
        if context.tax_component:
            search_text = f"{rate_value}% {context.tax_component}"

        query_vec = self._embedding.encode([search_text])[0]

        where_filter: dict[str, Any] | None = None
        if tax_rate_field.confidence >= self._settings.filter_threshold:
            where_filter = {"rate": rate_value}
            if context.tax_component:
                where_filter = {
                    "$and": [
                        {"rate": rate_value},
                        {"component": context.tax_component},
                    ]
                }

        strategy = (
            ResolutionStrategy.FILTERED_SEMANTIC
            if where_filter
            else ResolutionStrategy.PURE_SEMANTIC
        )

        results = self._vector.semantic_search(
            entity="tax_codes",
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=3,
            where=where_filter,
        )

        if not results:
            return FKMatch(strategy=ResolutionStrategy.NOT_FOUND)

        top = results[0]
        candidates = [
            {"erp_id": r["erp_id"], "score": r["score"], "metadata": r["metadata"]}
            for r in results
        ]

        return FKMatch(
            erp_id=top["erp_id"],
            matched_on=search_text,
            strategy=strategy,
            confidence=top["score"],
            candidates=candidates,
        )

    # ── generic helpers ──────────────────────────────────────────────

    def _resolve_fk(
        self,
        field: ScoredField,
        entity: str,
        tenant_id: str,
        erp_system: str,
        hard_key_meta: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> FKMatch:
        """Generic four-strategy FK resolution.

        Picks strategy based on field.confidence and delegates to
        hard_match or semantic_search on the vector service.
        """
        threshold = self._settings.hard_key_threshold
        filter_thresh = self._settings.filter_threshold
        value = str(field.value)

        # Strategy 1 — HARD_KEY
        if field.confidence >= threshold and hard_key_meta:
            meta = self._vector.hard_match(
                entity=entity,
                tenant_id=tenant_id,
                erp_system=erp_system,
                where={hard_key_meta: value},
            )
            if meta is not None:
                return FKMatch(
                    erp_id=meta["erp_id"],
                    matched_on=f"{hard_key_meta}={value}",
                    strategy=ResolutionStrategy.HARD_KEY,
                    confidence=1.0,
                )

        # Strategy 2 — FILTERED_SEMANTIC
        query_vec = self._embedding.encode([value])[0]
        where_filter = filters if (filters and field.confidence >= filter_thresh) else None
        strategy = (
            ResolutionStrategy.FILTERED_SEMANTIC
            if where_filter
            else ResolutionStrategy.PURE_SEMANTIC
        )

        results = self._vector.semantic_search(
            entity=entity,
            tenant_id=tenant_id,
            erp_system=erp_system,
            query_embedding=query_vec,
            n_results=3,
            where=where_filter,
        )

        if not results:
            return FKMatch(strategy=ResolutionStrategy.NOT_FOUND)

        top = results[0]
        candidates = [
            {"erp_id": r["erp_id"], "score": r["score"]}
            for r in results
        ]

        return FKMatch(
            erp_id=top["erp_id"],
            matched_on=value,
            strategy=strategy,
            confidence=top["score"],
            candidates=candidates,
        )
