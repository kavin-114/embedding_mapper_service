"""Context builder — constructs InvoiceContext from vendor resolution results."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from app.models.resolution import InvoiceContext

if TYPE_CHECKING:
    from app.config import Settings
    from app.services.vector_service import VectorService


class ContextBuilder:
    """Builds the InvoiceContext used by downstream resolvers (Stage 4).

    Takes the vendor match metadata, compares country/region for tax scope,
    and queries the vendor_context collection for historic item preferences.
    """

    def __init__(self, settings: "Settings") -> None:
        self._company_country = settings.company_country
        self._company_region_code = settings.company_region_code

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

        - Compare vendor country/region with company for tax scope.
        - Use vendor category as item_group_filter.
        - Query vendor_context collection for preferred items (if available).
        - Use verified tax_id from feedback history.
        """
        vendor_country = str(vendor_metadata.get("country", ""))
        vendor_region = str(vendor_metadata.get("region_code", ""))

        tax_scope = self.derive_tax_scope(
            vendor_country,
            vendor_region,
            self._company_country,
            self._company_region_code,
        )

        item_group = vendor_metadata.get("category") or vendor_metadata.get("item_group")

        # Higher vendor confidence → lower floor
        confidence_floor = max(0.40, 0.50 - (vendor_confidence - 0.70) * 0.33)

        # Query vendor history for preferred items and verified tax_id
        preferred_items: list[dict[str, Any]] = []
        verified_tax_id: str | None = None

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
                # Use the verified tax_id from the most frequent record
                verified_tax_id = history[0].get("vendor_tax_id")

        return InvoiceContext(
            vendor_known=True,
            vendor_erp_id=vendor_erp_id,
            tax_scope=tax_scope,
            tax_component=None,
            item_group_filter=str(item_group) if item_group else None,
            confidence_floor=confidence_floor,
            preferred_items=preferred_items,
            verified_tax_id=verified_tax_id,
        )

    @staticmethod
    def derive_tax_scope(
        vendor_country: str,
        vendor_region: str,
        company_country: str,
        company_region: str,
    ) -> str | None:
        """Derive tax scope from vendor vs company country/region.

        Returns:
            INTRA_REGION  — same country + same region
            INTER_REGION  — same country + different region
            IMPORT        — different country
            None          — insufficient data
        """
        if not vendor_country or not company_country:
            return None

        if vendor_country != company_country:
            return "IMPORT"

        if vendor_region and company_region:
            if vendor_region == company_region:
                return "INTRA_REGION"
            return "INTER_REGION"

        return None
