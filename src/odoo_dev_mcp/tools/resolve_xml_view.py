"""Tool 02: resolve_xml_view — merged view resolution for a model/view_type."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, json_col


async def resolve_xml_view(
    model_name: str,
    view_type: str,
    get_db: Callable[[], Path],
) -> dict:
    """Get the merged/resolved XML view for a model, showing all fields,
    buttons, and inherited view customizations."""
    db_path = get_db()

    # Include primary views of the requested type + inherited/xpath extensions.
    # Inherited views often have view_type='xpath' since their arch root is <xpath>.
    view_rows = await async_query(
        db_path,
        """
        SELECT * FROM views
        WHERE model = ?
          AND (view_type = ?
               OR view_type = 'xpath'
               OR view_type IS NULL
               OR inherit_id IS NOT NULL)
        ORDER BY inherit_id NULLS FIRST, priority ASC
        """,
        (model_name, view_type),
    )

    if not view_rows:
        return {
            "error": f"No '{view_type}' views found for model '{model_name}'.",
            "model": model_name,
            "view_type": view_type,
        }

    views = []
    all_fields: list[str] = []
    all_buttons: list[str] = []

    for row in view_rows:
        field_names = json_col(row, "field_names", [])
        button_names = json_col(row, "button_names", [])
        is_primary = row.get("inherit_id") is None

        view_entry: dict = {
            "xml_id": row.get("xml_id"),
            "name": row.get("name"),
            "module": row.get("module_name"),
            "file": row.get("file_path"),
            "priority": row.get("priority"),
            "inherit_id": row.get("inherit_id"),
            "is_primary": is_primary,
        }
        if is_primary:
            view_entry["fields"] = field_names
            view_entry["buttons"] = button_names
        else:
            # For inherit views, compute what was added vs base
            view_entry["fields_added"] = field_names
            view_entry["buttons_added"] = button_names

        views.append(view_entry)

        for f in field_names:
            if f and f not in all_fields:
                all_fields.append(f)
        for b in button_names:
            if b and b not in all_buttons:
                all_buttons.append(b)

    return {
        "model": model_name,
        "view_type": view_type,
        "views": views,
        "all_fields": all_fields,
        "all_buttons": all_buttons,
        "total_views": len(views),
    }
