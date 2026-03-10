"""Context builder — constructs InvoiceContext from vendor resolution results."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app.models.resolution import InvoiceContext

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.vector_service import VectorService


class ContextBuilder:
    """Builds the InvoiceContext used by downstream resolvers (Stage 4).

    Takes the vendor match metadata, compares state codes for tax component,
    and queries the vendor_context collection for historic item preferences.
    """

    def __init__(self, settings: "Settings") -> None:
        self._company_state_code = settings.company_state_code

    def build(
        self,
        vendor_metadata: dict[str, Any],
        vendor_erp_id: Any,
        vendor_confidence: float,
        vector_svc: "VectorService | None" = None,
        tenant_id: str | None = None,
        erp_system: str | None = None,
    ) -> InvoiceContext:
        """Build InvoiceContext from resolved vendor metadata.

        - Compare vendor state_code with company state_code for tax component.
        - Use vendor category as item_group_filter.
        - Query vendor_context collection for preferred items (if available).
        - Use verified GSTIN from feedback history.
        """
        vendor_state = str(vendor_metadata.get("state_code", ""))
        company_state = self._company_state_code

        if vendor_state and company_state:
            tax_component = (
                "CGST_SGST" if vendor_state == company_state else "IGST"
            )
        else:
            tax_component = None

        item_group = vendor_metadata.get("category") or vendor_metadata.get("item_group")

        # Higher vendor confidence → lower floor
        confidence_floor = max(0.40, 0.50 - (vendor_confidence - 0.70) * 0.33)

        # Query vendor history for preferred items and verified GSTIN
        preferred_items: list[dict[str, Any]] = []
        verified_gstin: str | None = None

        if vector_svc and tenant_id and erp_system and vendor_erp_id:
            history = vector_svc.get_vendor_context(
                tenant_id=tenant_id,
                erp_system=erp_system,
                vendor_erp_id=str(vendor_erp_id),
            )
            if history:
                preferred_items = [
                    {
                        "item_erp_id": h.get("item_erp_id"),
                        "item_code": h.get("item_code"),
                        "hsn_code": h.get("hsn_code"),
                        "description": h.get("description", ""),
                        "frequency": h.get("frequency", 1),
                    }
                    for h in history
                ]
                # Use the verified GSTIN from the most frequent record
                verified_gstin = history[0].get("vendor_gstin")

        return InvoiceContext(
            vendor_known=True,
            vendor_erp_id=vendor_erp_id,
            tax_component=tax_component,
            item_group_filter=str(item_group) if item_group else None,
            confidence_floor=confidence_floor,
            preferred_items=preferred_items,
            verified_gstin=verified_gstin,
        )

    @staticmethod
    def derive_tax_component_from_gstin(
        gstin: str,
        company_state_code: str,
    ) -> str | None:
        """Derive IGST/CGST_SGST from a GSTIN's first 2 digits."""
        if not gstin or len(gstin) < 2:
            return None

        vendor_state = gstin[:2]
        if not vendor_state.isdigit():
            return None

        if vendor_state == company_state_code:
            return "CGST_SGST"
        return "IGST"
