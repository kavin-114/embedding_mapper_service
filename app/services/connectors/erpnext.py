"""Frappe/ERPNext REST API client for pull-based sync.

Authentication uses ``Authorization: token api_key:api_secret``.

Frappe list API defaults:
  - ``limit_page_length=0`` fetches ALL records (no pagination).
  - ``fields`` must be a JSON array string like ``'["name","field"]'``.
  - Child tables (e.g. Address links) require fetching each doc individually.
"""

from __future__ import annotations

import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote

import httpx

from app.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT_CONCURRENCY = 10  # conservative for Frappe Cloud rate limits


class ERPNextClient:
    """Lightweight client for the Frappe REST API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        timeout: float = 30.0,
        max_workers: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        self._base = url.rstrip("/")
        self._headers = {"Authorization": f"token {api_key}:{api_secret}"}
        self._timeout = timeout
        self._max_workers = max_workers

    # ── low-level ────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}{endpoint}"
        resp = httpx.get(url, headers=self._headers, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _list(
        self,
        doctype: str,
        fields: list[str] | None = None,
        filters: dict[str, Any] | list | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Generic list fetch for any Frappe doctype."""
        params: dict[str, Any] = {"limit_page_length": limit or 0}
        if fields:
            params["fields"] = '["' + '","'.join(fields) + '"]'
        if filters:
            params["filters"] = _json.dumps(filters)
        data = self._get(f"/api/resource/{doctype}", params)
        return data.get("data", [])

    def _get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        """Fetch a single document with all fields and child tables."""
        safe_name = quote(name, safe="")
        data = self._get(f"/api/resource/{doctype}/{safe_name}")
        return data.get("data", {})

    def _get_docs_batch(
        self,
        doctype: str,
        names: list[str],
        label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch multiple documents concurrently using a thread pool."""
        tag = label or doctype
        total = len(names)
        if total == 0:
            return []

        results: list[dict[str, Any]] = []
        errors = 0

        def _fetch(name: str) -> dict[str, Any] | None:
            try:
                return self._get_doc(doctype, name)
            except Exception as e:
                logger.warning("erpnext.fetch_error", doctype=doctype, name=name, error=str(e))
                return None

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(_fetch, n): n for n in names}
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                doc = future.result()
                if doc:
                    results.append(doc)
                else:
                    errors += 1
                if done_count % 50 == 0 or done_count == total:
                    logger.info("erpnext.progress", entity=tag, done=done_count, total=total)

        if errors:
            logger.warning("erpnext.batch_errors", entity=tag, errors=errors, total=total)
        return results

    # ── Master data fetchers ─────────────────────────────────────────

    def get_suppliers(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Supplier docs. If *names* given, fetch only those."""
        if names is None:
            names_list = self._list("Supplier", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        suppliers = self._get_docs_batch("Supplier", names)
        logger.info("erpnext.fetched", doctype="Supplier", count=len(suppliers))
        return suppliers

    def get_items(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Item docs. If *names* given, fetch only those."""
        if names is None:
            names_list = self._list("Item", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        items = self._get_docs_batch("Item", names)
        logger.info("erpnext.fetched", doctype="Item", count=len(items))
        return items

    def get_tax_templates(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Item Tax Template with taxes child table."""
        if names is None:
            names_list = self._list("Item Tax Template", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        templates = self._get_docs_batch("Item Tax Template", names)
        logger.info("erpnext.fetched", doctype="Item Tax Template", count=len(templates))
        return templates

    def get_uoms(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch UOM doctypes."""
        if names is None:
            names_list = self._list("UOM", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        uoms = self._get_docs_batch("UOM", names)
        logger.info("erpnext.fetched", doctype="UOM", count=len(uoms))
        return uoms

    def get_companies(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Company doctypes with all fields."""
        if names is None:
            names_list = self._list("Company", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        companies = self._get_docs_batch("Company", names)
        logger.info("erpnext.fetched", doctype="Company", count=len(companies))
        return companies

    def get_addresses(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Address doctypes with Dynamic Link children."""
        if names is None:
            names_list = self._list("Address", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        addresses = self._get_docs_batch("Address", names)
        logger.info("erpnext.fetched", doctype="Address", count=len(addresses))
        return addresses

    def get_cost_centers(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Cost Center doctypes."""
        if names is None:
            names_list = self._list("Cost Center", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        cost_centers = self._get_docs_batch("Cost Center", names)
        logger.info("erpnext.fetched", doctype="Cost Center", count=len(cost_centers))
        return cost_centers

    def get_warehouses(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Warehouse doctypes."""
        if names is None:
            names_list = self._list("Warehouse", fields=["name"], limit=limit)
            names = [n["name"] for n in names_list]
        warehouses = self._get_docs_batch("Warehouse", names)
        logger.info("erpnext.fetched", doctype="Warehouse", count=len(warehouses))
        return warehouses

    def get_purchase_taxes_templates(self, limit: int = 0, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch Purchase Taxes and Charges Template doctypes."""
        if names is None:
            names_list = self._list(
                "Purchase Taxes and Charges Template", fields=["name"], limit=limit,
            )
            names = [n["name"] for n in names_list]
        templates = self._get_docs_batch("Purchase Taxes and Charges Template", names)
        logger.info(
            "erpnext.fetched",
            doctype="Purchase Taxes and Charges Template",
            count=len(templates),
        )
        return templates

    def get_purchase_invoice(self, name: str) -> dict[str, Any]:
        """Fetch a single Purchase Invoice with items child table."""
        return self._get_doc("Purchase Invoice", name)

    def get_purchase_invoices(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch Purchase Invoice list (summaries)."""
        return self._list("Purchase Invoice", filters=filters, limit=limit)

    def get_purchase_invoices_full(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 0,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Purchase Invoices with full detail (items child table).

        If *names* provided, fetch those directly. Otherwise list+fetch.
        """
        if names is None:
            summaries = self._list("Purchase Invoice", fields=["name"], filters=filters, limit=limit)
            names = [s["name"] for s in summaries]
        invoices = self._get_docs_batch("Purchase Invoice", names, label="Purchase Invoice")
        logger.info("erpnext.fetched", doctype="Purchase Invoice", count=len(invoices))
        return invoices

    def get_pi_names_with_attachments(self, limit: int = 0) -> list[str]:
        """Get Purchase Invoice names that have at least one file attached.

        Queries the File doctype filtered by attached_to_doctype=Purchase Invoice.
        """
        files = self._list(
            "File",
            fields=["attached_to_name"],
            filters={
                "attached_to_doctype": "Purchase Invoice",
                "is_private": 1,
            },
            limit=limit,
        )
        # Deduplicate — multiple files can be attached to the same PI
        names = list({f["attached_to_name"] for f in files if f.get("attached_to_name")})
        logger.info("erpnext.pi_with_attachments", count=len(names))
        return names
