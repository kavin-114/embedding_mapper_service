"""Convert ERPNext master data into seed-record format for ChromaDB.

Each function takes raw Frappe doctype dicts and returns a list of flat
dicts ready for ``VectorService.upsert()``.
"""

from __future__ import annotations

from typing import Any

# Country name/code → tax ID type mapping
_COUNTRY_TAX_ID: dict[str, str] = {
    "india": "GSTIN",
    "in": "GSTIN",
    "oman": "VAT",
    "om": "VAT",
    "united arab emirates": "VAT",
    "ae": "VAT",
    "saudi arabia": "VAT",
    "sa": "VAT",
    "united kingdom": "VAT",
    "gb": "VAT",
    "united states": "EIN",
    "us": "EIN",
}


def _detect_tax_id_type(tax_id: str, country: str) -> str:
    """Detect tax ID type based on country and tax_id pattern."""
    country_lower = country.strip().lower()
    known = _COUNTRY_TAX_ID.get(country_lower)
    if known:
        # For India, still validate the 15-digit GSTIN pattern
        if known == "GSTIN" and not (len(tax_id) == 15 and tax_id[:2].isdigit()):
            return "TIN"
        return known
    # Fallback: check GSTIN pattern regardless of country field
    if len(tax_id) == 15 and tax_id[:2].isdigit():
        return "GSTIN"
    return "TIN"


def suppliers_to_seed_records(suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Supplier doctypes to vendor seed records."""
    records = []
    for s in suppliers:
        name = s.get("supplier_name") or s.get("name", "")
        rec: dict[str, Any] = {
            "erp_id": s.get("name", ""),
            "text": name,
        }

        tax_id = s.get("tax_id") or s.get("gstin") or ""
        if tax_id:
            rec["tax_id"] = tax_id
            rec["tax_id_type"] = _detect_tax_id_type(tax_id, s.get("country", ""))

        if s.get("pan"):
            rec["pan"] = s["pan"]
        if s.get("supplier_group"):
            rec["category"] = s["supplier_group"]
        if s.get("country"):
            rec["country"] = s["country"]
        region = s.get("gst_state_number") or s.get("state") or s.get("gst_state")
        if region:
            rec["region_code"] = str(region)
        if s.get("city"):
            rec["city"] = s["city"]
        if s.get("pincode"):
            rec["pincode"] = s["pincode"]
        if s.get("supplier_type"):
            rec["supplier_type"] = s["supplier_type"]
        if s.get("default_currency"):
            rec["currency"] = s["default_currency"]

        rec["active"] = not s.get("disabled", False)

        if s.get("supplier_name") and s.get("supplier_name") != name:
            rec["trade_name"] = s["supplier_name"]

        records.append(rec)
    return records


def items_to_seed_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Item doctypes to item seed records."""
    records = []
    for item in items:
        rec: dict[str, Any] = {
            "erp_id": item.get("name", ""),
            "text": item.get("item_name", ""),
            "item_code": item.get("item_code") or item.get("name", ""),
        }
        if item.get("description"):
            rec["description"] = item["description"]
        if item.get("item_group"):
            rec["item_group"] = item["item_group"]
        if item.get("stock_uom"):
            rec["uom"] = item["stock_uom"]
        if item.get("gst_hsn_code"):
            rec["hsn_code"] = item["gst_hsn_code"]
        records.append(rec)
    return records


def tax_templates_to_seed_records(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Item Tax Template doctypes to tax_code seed records."""
    records = []
    for t in templates:
        template_name = t.get("name", "")
        rate = ""
        component = ""

        taxes = t.get("taxes", [])
        if taxes:
            first = taxes[0]
            rate = str(first.get("tax_rate", ""))
            account = first.get("account_head", "")
            if account:
                component = account.split(" - ")[0].strip()
        else:
            parts = template_name.replace("%", "").split("-")
            for part in parts:
                stripped = part.strip()
                try:
                    rate = str(float(stripped))
                except ValueError:
                    if stripped and not component:
                        component = stripped

        rec: dict[str, Any] = {
            "erp_id": template_name,
            "text": template_name,
        }
        if rate:
            rec["rate"] = rate
        if component:
            rec["component"] = component
        records.append(rec)
    return records


def uoms_to_seed_records(uoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext UOM doctypes to uom seed records."""
    records = []
    for u in uoms:
        name = u.get("name", "")
        records.append({
            "erp_id": name,
            "text": name,
            "uom_code": u.get("name", name),
        })
    return records


def companies_to_seed_records(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Company doctypes to company seed records."""
    records = []
    for c in companies:
        rec: dict[str, Any] = {
            "erp_id": c.get("name", ""),
            "text": c.get("company_name", c.get("name", "")),
            "company_name": c.get("company_name", ""),
        }
        if c.get("country"):
            rec["country"] = c["country"]
        if c.get("default_currency"):
            rec["default_currency"] = c["default_currency"]
        if c.get("abbr"):
            rec["abbr"] = c["abbr"]
        if c.get("default_cost_center"):
            rec["default_cost_center"] = c["default_cost_center"]
        if c.get("default_expense_account"):
            rec["default_expense_account"] = c["default_expense_account"]
        if c.get("default_payable_account"):
            rec["default_payable_account"] = c["default_payable_account"]
        rec["active"] = True
        records.append(rec)
    return records


def addresses_to_seed_records(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Address doctypes to address seed records.

    Extracts link_supplier / link_company from the Dynamic Links child table.
    """
    records = []
    for a in addresses:
        title = a.get("address_title", a.get("name", ""))
        rec: dict[str, Any] = {
            "erp_id": a.get("name", ""),
            "text": title,
            "address_title": title,
            "address_type": a.get("address_type", ""),
        }
        if a.get("address_line1"):
            rec["address_line1"] = a["address_line1"]
        if a.get("address_line2"):
            rec["address_line2"] = a["address_line2"]
        if a.get("city"):
            rec["city"] = a["city"]
        if a.get("state"):
            rec["state"] = a["state"]
        if a.get("country"):
            rec["country"] = a["country"]
        if a.get("pincode"):
            rec["pincode"] = a["pincode"]
        rec["is_primary_address"] = bool(a.get("is_primary_address", False))
        rec["is_shipping_address"] = bool(a.get("is_shipping_address", False))
        rec["disabled"] = bool(a.get("disabled", False))

        links = a.get("links", [])
        for link in links:
            dt = link.get("link_doctype", "")
            dn = link.get("link_name", "")
            if dt == "Supplier" and "link_supplier" not in rec:
                rec["link_supplier"] = dn
            elif dt == "Company" and "link_company" not in rec:
                rec["link_company"] = dn

        records.append(rec)
    return records


def cost_centers_to_seed_records(cost_centers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Cost Center doctypes to cost_center seed records."""
    records = []
    for cc in cost_centers:
        name = cc.get("name", "")
        rec: dict[str, Any] = {
            "erp_id": name,
            "text": cc.get("cost_center_name", name),
            "cost_center_name": cc.get("cost_center_name", name),
        }
        if cc.get("company"):
            rec["company"] = cc["company"]
        if cc.get("parent_cost_center"):
            rec["parent_cost_center"] = cc["parent_cost_center"]
        rec["is_group"] = bool(cc.get("is_group", False))
        rec["disabled"] = bool(cc.get("disabled", False))
        records.append(rec)
    return records


def warehouses_to_seed_records(warehouses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ERPNext Warehouse doctypes to warehouse seed records."""
    records = []
    for w in warehouses:
        name = w.get("name", "")
        rec: dict[str, Any] = {
            "erp_id": name,
            "text": w.get("warehouse_name", name),
            "warehouse_name": w.get("warehouse_name", name),
        }
        if w.get("warehouse_type"):
            rec["warehouse_type"] = w["warehouse_type"]
        if w.get("company"):
            rec["company"] = w["company"]
        rec["is_group"] = bool(w.get("is_group", False))
        rec["disabled"] = bool(w.get("disabled", False))
        records.append(rec)
    return records


def purchase_tax_templates_to_seed_records(
    templates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert ERPNext Purchase Taxes and Charges Template to seed records."""
    records = []
    for t in templates:
        name = t.get("name", "")
        rec: dict[str, Any] = {
            "erp_id": name,
            "text": t.get("title", name),
            "title": t.get("title", name),
        }
        if t.get("company"):
            rec["company"] = t["company"]
        if t.get("tax_category"):
            rec["tax_category"] = t["tax_category"]
        rec["disabled"] = bool(t.get("disabled", False))
        records.append(rec)
    return records


# ── Entity → fetcher + converter mapping ─────────────────────────────

# Maps entity name → (client_method_name, converter_function)
ENTITY_SYNC_MAP: dict[str, tuple[str, callable]] = {
    "vendors": ("get_suppliers", suppliers_to_seed_records),
    "items": ("get_items", items_to_seed_records),
    "tax_codes": ("get_tax_templates", tax_templates_to_seed_records),
    "uoms": ("get_uoms", uoms_to_seed_records),
    "companies": ("get_companies", companies_to_seed_records),
    "addresses": ("get_addresses", addresses_to_seed_records),
    "cost_centers": ("get_cost_centers", cost_centers_to_seed_records),
    "warehouses": ("get_warehouses", warehouses_to_seed_records),
    "tax_templates": ("get_purchase_taxes_templates", purchase_tax_templates_to_seed_records),
}
