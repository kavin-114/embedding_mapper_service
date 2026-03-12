"""Accuracy evaluation — compare mapper output against ground truth."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldResult:
    """Result of evaluating a single field mapping."""

    field_name: str
    expected_erp_id: str
    actual_erp_id: str | None
    correct: bool
    confidence: float = 0.0
    strategy: str | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "expected_erp_id": self.expected_erp_id,
            "actual_erp_id": self.actual_erp_id,
            "correct": self.correct,
            "confidence": self.confidence,
            "strategy": self.strategy,
            "status": self.status,
        }


@dataclass
class InvoiceResult:
    """Evaluation result for a single invoice."""

    invoice_number: str
    supplier: str
    field_results: list[FieldResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if not self.field_results:
            return 0.0
        correct = sum(1 for f in self.field_results if f.correct)
        return correct / len(self.field_results)

    @property
    def overall_status(self) -> str:
        acc = self.accuracy
        if acc >= 1.0:
            return "perfect"
        elif acc >= 0.8:
            return "good"
        elif acc >= 0.5:
            return "partial"
        return "poor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_number": self.invoice_number,
            "supplier": self.supplier,
            "accuracy": round(self.accuracy, 4),
            "overall_status": self.overall_status,
            "field_results": [f.to_dict() for f in self.field_results],
        }


@dataclass
class BacktestResult:
    """Aggregated evaluation result for a batch of invoices."""

    invoice_results: list[InvoiceResult] = field(default_factory=list)

    @property
    def overall_accuracy(self) -> float:
        all_fields = [f for inv in self.invoice_results for f in inv.field_results]
        if not all_fields:
            return 0.0
        return sum(1 for f in all_fields if f.correct) / len(all_fields)

    @property
    def by_field_type(self) -> dict[str, dict[str, Any]]:
        """Accuracy breakdown by field type (vendor, item, uom, tax)."""
        groups: dict[str, list[FieldResult]] = {}
        for inv in self.invoice_results:
            for f in inv.field_results:
                # Determine field type from field_name
                if "vendor" in f.field_name:
                    ft = "vendor"
                elif "description" in f.field_name:
                    ft = "item"
                elif "uom" in f.field_name:
                    ft = "uom"
                elif "tax" in f.field_name:
                    ft = "tax"
                else:
                    ft = "other"
                groups.setdefault(ft, []).append(f)

        result = {}
        for ft, fields in groups.items():
            correct = sum(1 for f in fields if f.correct)
            result[ft] = {
                "total": len(fields),
                "correct": correct,
                "accuracy": round(correct / len(fields), 4) if fields else 0.0,
            }
        return result

    @property
    def by_status(self) -> dict[str, dict[str, Any]]:
        """Accuracy breakdown by mapping status."""
        groups: dict[str, list[FieldResult]] = {}
        for inv in self.invoice_results:
            for f in inv.field_results:
                status = f.status or "unknown"
                groups.setdefault(status, []).append(f)

        result = {}
        for status, fields in groups.items():
            correct = sum(1 for f in fields if f.correct)
            result[status] = {
                "total": len(fields),
                "correct": correct,
                "accuracy": round(correct / len(fields), 4) if fields else 0.0,
            }
        return result

    @property
    def by_strategy(self) -> dict[str, dict[str, Any]]:
        """Accuracy breakdown by resolution strategy."""
        groups: dict[str, list[FieldResult]] = {}
        for inv in self.invoice_results:
            for f in inv.field_results:
                strat = f.strategy or "none"
                groups.setdefault(strat, []).append(f)

        result = {}
        for strat, fields in groups.items():
            correct = sum(1 for f in fields if f.correct)
            result[strat] = {
                "total": len(fields),
                "correct": correct,
                "accuracy": round(correct / len(fields), 4) if fields else 0.0,
            }
        return result

    @property
    def failures(self) -> list[dict[str, Any]]:
        """All incorrect field mappings."""
        out = []
        for inv in self.invoice_results:
            for f in inv.field_results:
                if not f.correct:
                    out.append({
                        "invoice": inv.invoice_number,
                        **f.to_dict(),
                    })
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_accuracy": round(self.overall_accuracy, 4),
            "invoice_count": len(self.invoice_results),
            "by_field_type": self.by_field_type,
            "by_status": self.by_status,
            "by_strategy": self.by_strategy,
            "failures": self.failures,
            "invoices": [inv.to_dict() for inv in self.invoice_results],
        }


class Evaluator:
    """Compares mapper output against ground truth."""

    def evaluate_invoice(
        self,
        map_response: dict[str, Any],
        ground_truth: dict[str, Any],
        invoice_number: str = "",
    ) -> InvoiceResult:
        """Evaluate a single invoice mapping against ground truth."""
        mappings = map_response.get("mappings", {})
        result = InvoiceResult(
            invoice_number=invoice_number or map_response.get("invoice_number", ""),
            supplier=ground_truth.get("vendor_name", ""),
        )

        # Vendor field
        vendor_mapping = mappings.get("vendor_name", {})
        result.field_results.append(FieldResult(
            field_name="vendor_name",
            expected_erp_id=ground_truth.get("vendor_erp_id", ""),
            actual_erp_id=vendor_mapping.get("erp_id"),
            correct=vendor_mapping.get("erp_id") == ground_truth.get("vendor_erp_id"),
            confidence=vendor_mapping.get("confidence", 0.0),
            strategy=vendor_mapping.get("strategy"),
            status=vendor_mapping.get("status"),
        ))

        # Line item fields
        gt_items = ground_truth.get("line_items", [])
        for idx, gt_item in enumerate(gt_items):
            prefix = f"line_items[{idx}]"

            # Item (description)
            desc_key = f"{prefix}.description"
            desc_mapping = mappings.get(desc_key, {})
            result.field_results.append(FieldResult(
                field_name=desc_key,
                expected_erp_id=gt_item.get("item_code", ""),
                actual_erp_id=desc_mapping.get("erp_id"),
                correct=desc_mapping.get("erp_id") == gt_item.get("item_code"),
                confidence=desc_mapping.get("confidence", 0.0),
                strategy=desc_mapping.get("strategy"),
                status=desc_mapping.get("status"),
            ))

            # UOM
            uom_key = f"{prefix}.uom"
            uom_mapping = mappings.get(uom_key, {})
            result.field_results.append(FieldResult(
                field_name=uom_key,
                expected_erp_id=gt_item.get("uom", ""),
                actual_erp_id=uom_mapping.get("erp_id"),
                correct=uom_mapping.get("erp_id") == gt_item.get("uom"),
                confidence=uom_mapping.get("confidence", 0.0),
                strategy=uom_mapping.get("strategy"),
                status=uom_mapping.get("status"),
            ))

            # Tax template
            tax_key = f"{prefix}.tax_rate"
            tax_mapping = mappings.get(tax_key, {})
            result.field_results.append(FieldResult(
                field_name=tax_key,
                expected_erp_id=gt_item.get("tax_template", ""),
                actual_erp_id=tax_mapping.get("erp_id"),
                correct=tax_mapping.get("erp_id") == gt_item.get("tax_template"),
                confidence=tax_mapping.get("confidence", 0.0),
                strategy=tax_mapping.get("strategy"),
                status=tax_mapping.get("status"),
            ))

        return result

    def evaluate_batch(self, results: list[InvoiceResult]) -> BacktestResult:
        """Aggregate individual invoice results into a BacktestResult."""
        return BacktestResult(invoice_results=results)
