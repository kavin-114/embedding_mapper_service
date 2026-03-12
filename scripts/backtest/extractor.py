"""Ground truth extraction from ERPNext Purchase Invoices."""


def extract_ground_truth(pi: dict) -> dict:
    """Extract expected erp_ids from an ERPNext Purchase Invoice.

    Returns a dict with:
      - vendor_erp_id: the supplier name (PI primary link)
      - line_items: list of {item_code, uom, tax_template} per PI item row
    """
    gt = {
        "vendor_erp_id": pi.get("supplier", ""),
        "vendor_name": pi.get("supplier_name", pi.get("supplier", "")),
        "company": pi.get("company", ""),
        "supplier_address": pi.get("supplier_address", ""),
        "shipping_address": pi.get("shipping_address", ""),
        "billing_address": pi.get("billing_address", ""),
        "cost_center": pi.get("cost_center", ""),
        "taxes_and_charges": pi.get("taxes_and_charges", ""),
        "set_warehouse": pi.get("set_warehouse", ""),
        "line_items": [],
    }

    for item in pi.get("items", []):
        gt["line_items"].append({
            "item_code": item.get("item_code", ""),
            "item_name": item.get("item_name", ""),
            "uom": item.get("uom", ""),
            "tax_template": item.get("item_tax_template", ""),
            "description": item.get("description", ""),
            "warehouse": item.get("warehouse", ""),
            "cost_center": item.get("cost_center", ""),
            "expense_account": item.get("expense_account", ""),
        })

    return gt
