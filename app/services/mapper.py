"""Mapper service — orchestrates the full invoice mapping pipeline."""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from app.logging_config import get_logger
from app.metrics import PipelineMetrics
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

logger = get_logger(__name__)


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
        metrics = PipelineMetrics()
        pipeline_start = time.perf_counter()

        logger.info(
            "pipeline.start",
            invoice_number=invoice.invoice_number,
            line_item_count=len(invoice.line_items),
        )

        # Stage 1 — Load ERP schema
        t0 = time.perf_counter()
        transformer = Transformer(erp_system)
        metrics.schema_load_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "stage.schema_loaded",
            erp_system=erp_system,
            elapsed_ms=round(metrics.schema_load_ms, 2),
        )

        # Stage 2 — Vendor resolution
        t0 = time.perf_counter()
        vendor_match, vendor_status = self._resolver.resolve_vendor(
            invoice, tenant_id, erp_system
        )
        metrics.vendor_resolution_ms = (time.perf_counter() - t0) * 1000

        metrics.vendor_strategy = vendor_match.strategy.value if vendor_match.strategy else None
        metrics.vendor_confidence = vendor_match.confidence
        metrics.vendor_status = vendor_status.value
        metrics.vendor_erp_id = str(vendor_match.erp_id) if vendor_match.erp_id else None

        logger.info(
            "stage.vendor_resolved",
            strategy=metrics.vendor_strategy,
            confidence=vendor_match.confidence,
            vendor_status=vendor_status.value,
            erp_id=vendor_match.erp_id,
            elapsed_ms=round(metrics.vendor_resolution_ms, 2),
        )

        # Stage 3 — Unknown vendor handler (if needed)
        if vendor_status == VendorStatus.NOT_FOUND:
            t0 = time.perf_counter()
            vendor_match, vendor_status, context = self._resolver.handle_unknown_vendor(
                invoice, tenant_id, erp_system
            )
            metrics.unknown_vendor_ms = (time.perf_counter() - t0) * 1000

            logger.info(
                "stage.unknown_vendor",
                status=vendor_status.value,
                elapsed_ms=round(metrics.unknown_vendor_ms, 2),
            )
        # Stage 4 — Context enrichment (vendor found path)
        elif vendor_status == VendorStatus.FOUND and vendor_match.erp_id:
            t0 = time.perf_counter()
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
            metrics.context_enrichment_ms = (time.perf_counter() - t0) * 1000

            logger.info(
                "stage.context_enriched",
                tax_scope=context.tax_scope,
                preferred_items_count=len(context.preferred_items),
                elapsed_ms=round(metrics.context_enrichment_ms, 2),
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

        # Stage 4b — Resolve company, addresses, cost_center, warehouse, tax template
        company_match = self._resolver.resolve_company(
            invoice.company_name, tenant_id, erp_system,
        )
        company_erp_id = str(company_match.erp_id) if company_match.erp_id else None

        # Populate company defaults on context from company metadata
        if company_match.candidates:
            cmeta = company_match.candidates[0].get("metadata", {})
        elif company_match.erp_id:
            cmeta = self._vector.hard_match(
                "companies", tenant_id, erp_system,
                {"company_name": str(company_match.erp_id)},
            ) or {}
        else:
            cmeta = {}

        context.company_erp_id = company_erp_id
        context.default_cost_center = cmeta.get("default_cost_center")
        context.default_expense_account = cmeta.get("default_expense_account")
        context.default_payable_account = cmeta.get("default_payable_account")

        # Resolve addresses
        supplier_address_match = FKMatch(strategy=ResolutionStrategy.NOT_FOUND)
        shipping_address_match = FKMatch(strategy=ResolutionStrategy.NOT_FOUND)
        billing_address_match = FKMatch(strategy=ResolutionStrategy.NOT_FOUND)

        if vendor_match.erp_id:
            supplier_address_match = self._resolver.resolve_address(
                str(vendor_match.erp_id), "link_supplier", tenant_id, erp_system,
            )
            shipping_address_match = self._resolver.resolve_address(
                str(vendor_match.erp_id), "link_supplier", tenant_id, erp_system,
                is_shipping=True,
            )
        if company_erp_id:
            billing_address_match = self._resolver.resolve_address(
                company_erp_id, "link_company", tenant_id, erp_system,
            )

        # Resolve cost center
        cost_center_match = self._resolver.resolve_cost_center(
            company_erp_id, tenant_id, erp_system,
        )
        if cost_center_match.strategy == ResolutionStrategy.NOT_FOUND and context.default_cost_center:
            cost_center_match = FKMatch(
                erp_id=context.default_cost_center,
                matched_on="company_default",
                strategy=ResolutionStrategy.HARD_KEY,
                confidence=1.0,
            )

        # Resolve warehouse
        warehouse_match = self._resolver.resolve_warehouse(
            company_erp_id, tenant_id, erp_system,
        )
        context.default_warehouse = str(warehouse_match.erp_id) if warehouse_match.erp_id else None

        # Resolve taxes and charges template
        tax_template_match = self._resolver.resolve_tax_template(
            company_erp_id, context.tax_component, tenant_id, erp_system,
        )

        logger.info(
            "stage.context_defaults_resolved",
            company=company_erp_id,
            supplier_address=supplier_address_match.erp_id,
            billing_address=billing_address_match.erp_id,
            cost_center=cost_center_match.erp_id,
            warehouse=warehouse_match.erp_id,
            tax_template=tax_template_match.erp_id,
        )

        # Stage 5 — Line item resolution
        t0 = time.perf_counter()
        resolved_lines: list[ResolvedLineItem] = []
        metrics.line_item_count = len(invoice.line_items)

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

            logger.debug(
                "stage.line_item_resolved",
                index=idx,
                item_strategy=item_match.strategy.value,
                item_confidence=item_match.confidence,
                uom_strategy=uom_match.strategy.value,
                uom_confidence=uom_match.confidence,
                tax_strategy=tax_match.strategy.value,
                tax_confidence=tax_match.confidence,
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

        metrics.line_items_ms = (time.perf_counter() - t0) * 1000

        # Build mappings, unresolved, review_required
        threshold = options.confidence_threshold
        mappings, unresolved, review_required = self._build_mappings(
            vendor_match, vendor_status, resolved_lines, threshold, metrics,
        )

        # Count line item statuses
        for rl in resolved_lines:
            for field_name, match in [("description", rl.item), ("uom", rl.uom), ("tax_rate", rl.tax)]:
                key = f"line_items[{rl.index}].{field_name}"
                detail = mappings.get(key)
                if detail:
                    metrics.count_status(detail.status.value)

        logger.info(
            "stage.line_items_complete",
            count=metrics.line_item_count,
            auto_map=metrics.auto_map_count,
            suggest=metrics.suggest_count,
            review=metrics.review_count,
            no_match=metrics.no_match_count,
            elapsed_ms=round(metrics.line_items_ms, 2),
        )

        # Remove candidates if not requested
        if not options.return_candidates:
            for detail in mappings.values():
                detail.candidates = []

        # Stage 6 — Transform payload
        t0 = time.perf_counter()
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
                "company": company_match.erp_id,
                "supplier_address": supplier_address_match.erp_id,
                "shipping_address": shipping_address_match.erp_id,
                "billing_address": billing_address_match.erp_id,
                "cost_center": cost_center_match.erp_id,
                "taxes_and_charges": tax_template_match.erp_id,
                "credit_to": context.default_payable_account,
            }

            transformed_lines = []
            for rl in resolved_lines:
                transformed_lines.append({
                    "resolved_ids": {
                        "description": rl.item.erp_id,
                        "uom": rl.uom.erp_id,
                        "tax_rate": rl.tax.erp_id,
                        "warehouse": context.default_warehouse,
                        "expense_account": context.default_expense_account,
                        "cost_center": cost_center_match.erp_id,
                    },
                    "raw": rl.raw,
                })

            erp_payload = transformer.transform(
                canonical_data, resolved_ids, transformed_lines,
            )

        metrics.transform_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "stage.transform_complete",
            elapsed_ms=round(metrics.transform_ms, 2),
        )

        # Determine overall status
        if unresolved:
            overall = "failed" if len(unresolved) > len(mappings) / 2 else "partial"
        elif review_required:
            overall = "partial"
        else:
            overall = "success"

        metrics.total_ms = (time.perf_counter() - pipeline_start) * 1000

        logger.info(
            "pipeline.complete",
            overall_status=overall,
            total_ms=round(metrics.total_ms, 2),
            metrics=metrics.to_dict(),
        )

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
        metrics: PipelineMetrics | None = None,
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

        if metrics:
            metrics.add_field(
                "vendor_name",
                strategy=vendor_detail.strategy,
                confidence=vendor_detail.confidence,
                status=vendor_detail.status.value,
                erp_id=str(vendor_detail.erp_id) if vendor_detail.erp_id else None,
            )

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

                if metrics:
                    metrics.add_field(
                        key,
                        strategy=detail.strategy,
                        confidence=detail.confidence,
                        status=detail.status.value,
                        erp_id=str(detail.erp_id) if detail.erp_id else None,
                    )

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
