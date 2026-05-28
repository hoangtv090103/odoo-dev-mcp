"""Tool 01: get_model_schema — full field/inherit schema for an Odoo model."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def get_model_schema(
    model_name: str,
    get_db: Callable[[], Path],
    compact: bool = False,
    fields_limit: int = 200,
) -> dict:
    """Get complete schema for an Odoo model: fields, types, compute methods,
    inheritance chain, and related models.

    Args:
        model_name:   Odoo model technical name (e.g. 'sale.order').
        get_db:       Callable returning the SQLite db Path.
        compact:      When True, collapse each field to a single descriptive
                      string instead of a full object.  ~10× fewer tokens;
                      ideal for an AI quick-scan before drilling in.
        fields_limit: Maximum number of fields to return (default 200).
                      Increase when you need the full list on very large models.
    """
    db_path = get_db()

    # Fetch primary model row first, then all rows for this model name
    all_model_rows = await async_query(
        db_path,
        "SELECT * FROM models WHERE name = ? ORDER BY inherit_type",
        (model_name,),
    )

    if not all_model_rows:
        return {
            "error": f"Model '{model_name}' not found in the index.",
            "model": model_name,
        }

    # Primary row: prefer inherit_type='primary', else first row
    primary_row = next(
        (r for r in all_model_rows if r.get("inherit_type") == "primary"),
        all_model_rows[0],
    )

    # Fetch all fields (honour fields_limit)
    field_rows = await async_query(
        db_path,
        "SELECT * FROM fields WHERE model_name = ? ORDER BY field_name LIMIT ?",
        (model_name, fields_limit),
    )
    total_fields = await async_query_one(
        db_path,
        "SELECT COUNT(*) AS cnt FROM fields WHERE model_name = ?",
        (model_name,),
    )
    total_fields_count: int = (total_fields or {}).get("cnt", len(field_rows))

    if compact:
        # One compact string per field: "name: Type[(comodel)] [flags]"
        fields: list = []
        for f in field_rows:
            parts = [f"{f.get('field_name')}: {f.get('field_type', '?')}"]
            comodel = f.get("comodel_name")
            if comodel:
                parts[0] += f"({comodel})"
            flags = []
            if f.get("required"):
                flags.append("required")
            if f.get("compute"):
                store_flag = "stored" if f.get("store", 1) else "unstored"
                flags.append(f"compute={f['compute']}[{store_flag}]")
            elif f.get("related"):
                flags.append(f"related={f['related']}")
            if f.get("readonly"):
                flags.append("readonly")
            if f.get("groups"):
                flags.append(f"groups={f['groups']}")
            if flags:
                parts.append(f"[{', '.join(flags)}]")
            fields.append(" ".join(parts))
    else:
        fields = []
        for f in field_rows:
            fields.append(
                {
                    "name": f.get("field_name"),
                    "type": f.get("field_type"),
                    "string": f.get("string_label"),
                    "required": bool(f.get("required", 0)),
                    "readonly": bool(f.get("readonly", 0)),
                    "store": bool(f.get("store", 1)),
                    "compute": f.get("compute"),
                    "related": f.get("related"),
                    "comodel": f.get("comodel_name"),
                    "groups": f.get("groups"),
                    "help": f.get("help_text"),
                    "module": f.get("module_name"),
                }
            )

    # Build extensions list: rows that are _inherit (not the primary definition)
    extensions = []
    for row in all_model_rows:
        if row.get("inherit_type") == "_inherit":
            # Get added fields from this module
            ext_fields = await async_query(
                db_path,
                "SELECT field_name FROM fields WHERE model_name = ? AND module_name = ?",
                (model_name, row.get("module_name")),
            )
            extensions.append(
                {
                    "module": row.get("module_name"),
                    "file": row.get("file_path"),
                    "line": row.get("line_number"),
                    "added_fields": [r["field_name"] for r in ext_fields],
                }
            )

    result = {
        "model": model_name,
        "class_name": primary_row.get("python_class"),
        "description": primary_row.get("description"),
        "inherit_type": primary_row.get("inherit_type"),
        "inherit_model": primary_row.get("inherit_model"),
        "inherits_map": json_col(primary_row, "inherits_map", {}),
        "is_abstract": bool(primary_row.get("abstract", 0)),
        "is_transient": bool(primary_row.get("transient", 0)),
        "module": primary_row.get("module_name"),
        "file": primary_row.get("file_path"),
        "line": primary_row.get("line_number"),
        "fields": fields,
        "extensions": extensions,
        "total_fields": total_fields_count,
        "compact": compact,
    }

    if total_fields_count > fields_limit:
        result["truncated"] = True
        result["truncated_note"] = (
            f"Showing {fields_limit}/{total_fields_count} fields. "
            f"Pass fields_limit={total_fields_count} to see all."
        )

    return result
