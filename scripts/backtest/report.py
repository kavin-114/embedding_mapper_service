"""Report generation — console, JSON, and CSV output."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from scripts.backtest.evaluator import BacktestResult


def print_summary(result: BacktestResult) -> None:
    """Print a formatted console summary of backtest results."""
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Invoices evaluated: {len(result.invoice_results)}")
    print(f"  Overall accuracy:   {result.overall_accuracy:.1%}")
    print()

    # By field type
    print("  By Field Type:")
    print(f"  {'Type':<12} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("  " + "-" * 40)
    for ft, stats in sorted(result.by_field_type.items()):
        print(
            f"  {ft:<12} {stats['correct']:>8} {stats['total']:>8} "
            f"{stats['accuracy']:>9.1%}"
        )
    print()

    # By strategy
    print("  By Strategy:")
    print(f"  {'Strategy':<20} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("  " + "-" * 48)
    for strat, stats in sorted(result.by_strategy.items()):
        print(
            f"  {strat:<20} {stats['correct']:>8} {stats['total']:>8} "
            f"{stats['accuracy']:>9.1%}"
        )
    print()

    # By status
    print("  By Status:")
    print(f"  {'Status':<16} {'Correct':>8} {'Total':>8} {'Accuracy':>10}")
    print("  " + "-" * 44)
    for status, stats in sorted(result.by_status.items()):
        print(
            f"  {status:<16} {stats['correct']:>8} {stats['total']:>8} "
            f"{stats['accuracy']:>9.1%}"
        )
    print()

    # Top failures
    failures = result.failures
    if failures:
        print(f"  Top Failures ({min(len(failures), 10)} of {len(failures)}):")
        print(f"  {'Invoice':<20} {'Field':<30} {'Expected':<15} {'Got':<15}")
        print("  " + "-" * 80)
        for f in failures[:10]:
            print(
                f"  {f['invoice']:<20} {f['field_name']:<30} "
                f"{f['expected_erp_id']:<15} {str(f['actual_erp_id'] or 'None'):<15}"
            )
    else:
        print("  No failures!")

    print("=" * 60 + "\n")


def save_json_report(result: BacktestResult, path: str) -> str:
    """Save full BacktestResult as JSON. Returns the output path."""
    out_path = Path(path).with_suffix(".json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    return str(out_path)


def save_csv_report(result: BacktestResult, path: str) -> str:
    """Save per-field results as CSV rows. Returns the output path."""
    out_path = Path(path).with_suffix(".csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    headers = [
        "invoice_number", "supplier", "field_name",
        "expected_erp_id", "actual_erp_id", "correct",
        "confidence", "strategy", "status", "skipped",
    ]

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for inv in result.invoice_results:
            for fr in inv.field_results:
                writer.writerow({
                    "invoice_number": inv.invoice_number,
                    "supplier": inv.supplier,
                    **fr.to_dict(),
                })

    return str(out_path)
