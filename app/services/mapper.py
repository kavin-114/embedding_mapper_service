"""Mapper service — orchestrates the full invoice mapping pipeline."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app.models.canonical import CanonicalInvoice, ScoredField
from app.models.resolution import (
    FKMatch,
    InvoiceContext,
    ResolvedLineItem,
    ResolutionStrategy,
    VendorStatus,
)
from app.models.response import MapResponse, MappingDetail, MappingStatus
from app.services.context_builder import ContextBuilder
from app.services.embedding_service import EmbeddingService
from app.services.resolver import Resolver
from app.services.transformer import Transformer
from app.services.vector_service import VectorService

if TYPE_CHECKING:
    from app.config import Settings
    from app.routers.map import MapOptions


class MapperService:
    """Top-level orchestrator that drives Stages 1–6 for a single invoice."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._embedding = EmbeddingService(settings)
        self._vector = VectorService(settings)
        self._resolver = Resolver(settings, self._embedding, self._vector)
        self._context_builder = ContextBuilder(settings)

    def map(
        self,
        invoice: CanonicalInvoice,
        erp_system: str,
        tenant_id: str,
        options: "MapOptions",
    ) -> MapResponse:
        """Run the full mapping pipeline for one invoice.

        Stages 1–6 executed sequentially.
        """
        # Stage 1 — Load ERP schema
        transformer = Transformer(erp_system)

        # Stage 2 — Vendor resolution
        vendor_match, vendor_status = self._resolver.resolve_vendor(
            invoice, tenant_id, erp_system
        )

        # Stage 3 — Unknown vendor handler (if needed)
        if vendor_status == VendorStatus.NOT_FOUND:
            vendor_match, vendor_status, context = self._resolver.handle_unknown_vendor(
                invoice, tenant_id, erp_system
            )
        # Stage 4 — Context enrichment (vendor found path)
        elif vendor_status == VendorStatus.FOUND and vendor_match.erp_id:
            # Retrieve vendor metadata for context building
            vendor_meta = {}
            if vendor_match.candidates:
                vendor_meta = vendor_match.candidates[0].get("metadata", {})
            elif vendor_match.strategy == ResolutionStrategy.HARD_KEY:
                # Hard key match — fetch metadata from collection
                tax_id_field = invoice.vendor_tax_id
                if tax_id_field:
                    fetched = self._vector.hard_match(
                        "vendors", tenant_id, erp_system,
                        {"tax_id": str(tax_id_field.value)},
                    )
                    if fetched:
                        vendor_meta = fetched

            context = self._context_builder.build(
                vendor_metadata=vendor_meta,
                vendor_erp_id=vendor_match.erp_id,
                vendor_confidence=vendor_match.confidence,
                vector_svc=self._vector,
                tenant_id=tenant_id,
                erp_system=erp_system,
            )
        else:
            # SUGGEST / REVIEW — partial context
            context = InvoiceContext(
                vendor_known=False,
                vendor_erp_id=vendor_match.erp_id,
                tax_scope=None,
                tax_component=None,
                confidence_floor=0.50,
            )

        # Map generic tax_scope → ERP-specific tax_component via schema
        if context.tax_scope and not context.tax_component:
            scope_map = transformer.get_tax_scope_map()
            context.tax_component = scope_map.get(context.tax_scope)

        # Stage 5 — Line item resolution
        resolved_lines: list[ResolvedLineItem] = []
        for idx, li in enumerate(invoice.line_items):
            item_fields: dict[str, ScoredField | None] = {
                "description": li.description,
                "hsn_code": li.hsn_code,
                "uom": li.uom,
                "item_code": li.item_code,
            }
            item_match = self._resolver.resolve_item(
                item_fields, context, tenant_id, erp_system
            )
            uom_match = self._resolver.resolve_uom(
                li.uom, tenant_id, erp_system
            )
            tax_match = self._resolver.resolve_tax(
                li.tax_rate, context, tenant_id, erp_system
            )

            raw_data: dict[str, Any] = {
                "description": str(li.description.value),
                "quantity": li.quantity,
                "unit_price": li.unit_price,
            }

            resolved_lines.append(ResolvedLineItem(
                index=idx,
                item=item_match,
                uom=uom_match,
                tax=tax_match,
                raw=raw_data,
            ))

        # Build mappings, unresolved, review_required
        threshold = options.confidence_threshold
        mappings, unresolved, review_required = self._build_mappings(
            vendor_match, vendor_status, resolved_lines, threshold,
        )

        # Remove candidates if not requested
        if not options.return_candidates:
            for detail in mappings.values():
                detail.candidates = []

        # Stage 6 — Transform payload
        erp_payload: dict[str, Any] | None = None
        if not options.dry_run:
            canonical_data = {
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date,
                "currency": invoice.currency,
                "total_amount": invoice.total_amount,
            }
            resolved_ids: dict[str, Any] = {
                "vendor_name": vendor_match.erp_id,
            }

            transformed_lines = []
            for rl in resolved_lines:
                transformed_lines.append({
                    "resolved_ids": {
                        "description": rl.item.erp_id,
                        "uom": rl.uom.erp_id,
                        "tax_rate": rl.tax.erp_id,
                    },
                    "raw": rl.raw,
                })

            erp_payload = transformer.transform(
                canonical_data, resolved_ids, transformed_lines,
            )

        # Determine overall status
        if unresolved:
            overall = "failed" if len(unresolved) > len(mappings) / 2 else "partial"
        elif review_required:
            overall = "partial"
        else:
            overall = "success"

        return MapResponse(
            status=overall,
            erp_payload=erp_payload,
            mappings=mappings,
            unresolved=unresolved,
            review_required=review_required,
        )

    def _build_mappings(
        self,
        vendor_match: FKMatch,
        vendor_status: VendorStatus,
        resolved_lines: list[ResolvedLineItem],
        threshold: float,
    ) -> tuple[dict[str, MappingDetail], list[str], list[str]]:
        """Convert resolution results into MappingDetail entries."""
        mappings: dict[str, MappingDetail] = {}
        unresolved: list[str] = []
        review_required: list[str] = []

        # Vendor mapping
        vendor_detail = self._fk_to_detail(vendor_match, threshold, "vendor_name")
        if vendor_status == VendorStatus.NOT_FOUND:
            vendor_detail.flags.append("VENDOR_NOT_FOUND")
        elif vendor_status == VendorStatus.STALE_DATA:
            vendor_detail.flags.append("STALE_DATA")
        elif vendor_status == VendorStatus.SUGGEST:
            vendor_detail.flags.append("SUGGEST")

        mappings["vendor_name"] = vendor_detail
        if vendor_detail.status == MappingStatus.NO_MATCH:
            unresolved.append("vendor_name")
        elif vendor_detail.status == MappingStatus.REVIEW:
            review_required.append("vendor_name")

        # Line item mappings
        for rl in resolved_lines:
            prefix = f"line_items[{rl.index}]"

            for field_name, match in [
                ("description", rl.item),
                ("uom", rl.uom),
                ("tax_rate", rl.tax),
            ]:
                key = f"{prefix}.{field_name}"
                detail = self._fk_to_detail(match, threshold, key)
                mappings[key] = detail

                if detail.status == MappingStatus.NO_MATCH:
                    unresolved.append(key)
                elif detail.status == MappingStatus.REVIEW:
                    review_required.append(key)

        return mappings, unresolved, review_required

    def _fk_to_detail(
        self,
        match: FKMatch,
        threshold: float,
        field_name: str,
    ) -> MappingDetail:
        """Convert an FKMatch to a MappingDetail with status classification."""
        if match.strategy == ResolutionStrategy.NOT_FOUND:
            status = MappingStatus.NO_MATCH
        elif match.confidence >= threshold:
            status = MappingStatus.AUTO_MAP
        elif match.confidence >= self._settings.suggest_threshold:
            status = MappingStatus.SUGGEST
        elif match.confidence >= self._settings.review_threshold:
            status = MappingStatus.REVIEW
        else:
            status = MappingStatus.NO_MATCH

        return MappingDetail(
            status=status,
            erp_id=match.erp_id,
            confidence=match.confidence,
            strategy=match.strategy.value if match.strategy else None,
            candidates=match.candidates,
            flags=[],
        )
