"""Transformer — converts resolved canonical data into ERP-specific payloads."""

from __future__ import annotations

from typing import Any

import yaml

from app.config import SCHEMAS_DIR


class Transformer:
    """Reads the ERP YAML schema and reshapes a resolved invoice into
    the target ERP's native payload format (Stage 6).
    """

    def __init__(self, erp_system: str) -> None:
        self._erp_system = erp_system
        self._schema = self._load_schema(erp_system)

    @staticmethod
    def _load_schema(erp_system: str) -> dict[str, Any]:
        """Load and parse the YAML schema for the given ERP."""
        path = SCHEMAS_DIR / f"{erp_system}.yaml"
        with open(path) as f:
            return yaml.safe_load(f)

    def get_field_map(self) -> dict[str, str]:
        """Return the top-level canonical → ERP field mapping."""
        return self._schema.get("field_map", {})

    def get_line_item_map(self) -> dict[str, str]:
        """Return the line-item canonical → ERP field mapping."""
        return self._schema.get("line_item_map", {})

    def get_fk_fields(self) -> dict[str, Any]:
        """Return FK field definitions from the schema."""
        return self._schema.get("fk_fields", {})

    def get_line_items_key(self) -> str:
        """Return the ERP-specific key name for the line items array."""
        return self._schema.get("line_items_key", "items")

    def get_id_type(self) -> str:
        """Return the ERP's ID type (name_string | integer | long_string)."""
        return self._schema.get("id_type", "name_string")

    def transform(
        self,
        canonical_data: dict[str, Any],
        resolved_ids: dict[str, Any],
        resolved_line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Transform a resolved canonical invoice into the ERP-native payload.

        Steps:
          1. Rename top-level keys using field_map.
          2. Inject resolved erp_ids into FK fields.
          3. Reshape line items using line_item_map.
          4. Handle ERP-specific structures (Odoo ORM tuples).
        """
        field_map = self.get_field_map()
        line_item_map = self.get_line_item_map()
        line_items_key = self.get_line_items_key()

        payload: dict[str, Any] = {}

        # Step 1+2 — rename top-level fields, inject FK IDs
        for canonical_key, erp_key in field_map.items():
            if canonical_key in resolved_ids and resolved_ids[canonical_key] is not None:
                payload[erp_key] = self._cast_id(resolved_ids[canonical_key])
            elif canonical_key in canonical_data:
                value = canonical_data[canonical_key]
                # Convert date objects to ISO string
                if hasattr(value, "isoformat"):
                    value = value.isoformat()
                payload[erp_key] = value

        # Step 3 — reshape line items
        erp_lines = []
        for line in resolved_line_items:
            erp_line: dict[str, Any] = {}
            for canonical_key, erp_key in line_item_map.items():
                if canonical_key in line.get("resolved_ids", {}):
                    erp_line[erp_key] = self._cast_id(
                        line["resolved_ids"][canonical_key]
                    )
                elif canonical_key in line.get("raw", {}):
                    erp_line[erp_key] = line["raw"][canonical_key]
            erp_lines.append(erp_line)

        payload[line_items_key] = erp_lines

        # Step 4 — Odoo ORM tuples
        if self._erp_system == "odoo":
            payload = self._apply_odoo_tuples(payload)

        return payload

    def _cast_id(self, erp_id: Any) -> Any:
        """Cast an erp_id to the correct type for this ERP."""
        id_type = self.get_id_type()
        if erp_id is None:
            return None
        if id_type == "integer":
            try:
                return int(erp_id)
            except (ValueError, TypeError):
                return erp_id
        # name_string and long_string → keep as string
        return str(erp_id)

    def _apply_odoo_tuples(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Wrap Odoo FK values in ORM command tuples where needed.

        orm_tuples config:
          - m2o fields → plain integer
          - [6, 0] fields → [(6, 0, [ids])]
        """
        orm_config = self._schema.get("orm_tuples", {})
        if not orm_config:
            return payload

        line_items_key = self.get_line_items_key()

        for field_name, rule in orm_config.items():
            # Apply to top-level payload
            if field_name in payload and payload[field_name] is not None:
                payload[field_name] = self._wrap_orm(payload[field_name], rule)

            # Apply to line items
            if line_items_key in payload:
                for line in payload[line_items_key]:
                    if field_name in line and line[field_name] is not None:
                        line[field_name] = self._wrap_orm(line[field_name], rule)

        return payload

    @staticmethod
    def _wrap_orm(value: Any, rule: Any) -> Any:
        """Apply a single ORM tuple rule to a value."""
        if rule == "m2o":
            # Many-to-one: plain integer ID
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        elif isinstance(rule, list) and len(rule) == 2:
            # Many-to-many: (cmd, arg, [ids])
            cmd, arg = rule
            if isinstance(value, list):
                ids = [int(v) for v in value]
            else:
                ids = [int(value)]
            return [(cmd, arg, ids)]
        return value
