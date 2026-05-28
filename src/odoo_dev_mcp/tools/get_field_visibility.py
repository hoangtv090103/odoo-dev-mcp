"""Tool 10: get_field_visibility — when a field is visible/required/readonly across views."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def get_field_visibility(
    model_name: str,
    field_name: str,
    get_db: Callable[[], Path],
) -> dict:
    """Get when a field is visible, required, or readonly across all views,
    including group restrictions and state-based attrs."""
    db_path = get_db()

    # Python-level field definition
    field_row = await async_query_one(
        db_path,
        """
        SELECT field_type, string_label, required, readonly, store,
               groups, states_visibility, compute, related,
               module_name, file_path, line_number
        FROM fields
        WHERE model_name = ? AND field_name = ?
        ORDER BY module_name
        LIMIT 1
        """,
        (model_name, field_name),
    )

    if not field_row:
        return {
            "error": f"Field '{field_name}' not found on model '{model_name}'.",
            "model": model_name,
            "field": field_name,
        }

    python_level = {
        "required": bool(field_row.get("required", 0)),
        "readonly": bool(field_row.get("readonly", 0)),
        "groups": field_row.get("groups"),
        "states_visibility": json_col(field_row, "states_visibility", {}),
        "compute": field_row.get("compute"),
        "related": field_row.get("related"),
        "file": field_row.get("file_path"),
        "line": field_row.get("line_number"),
    }

    # View-level references via view_element_refs
    ver_rows = await async_query(
        db_path,
        """
        SELECT ver.view_xml_id, ver.widget_name, ver.field_attrs,
               ver.groups_attr, ver.invisible_expr, ver.attrs_expr,
               ver.parent_element, v.view_type, v.module_name
        FROM view_element_refs ver
        JOIN views v ON v.xml_id = ver.view_xml_id
        WHERE ver.field_name = ? AND v.model = ?
        ORDER BY v.view_type, ver.view_xml_id
        """,
        (field_name, model_name),
    )

    view_level = []
    for row in ver_rows:
        entry: dict = {
            "view_xml_id": row.get("view_xml_id"),
            "view_type": row.get("view_type"),
            "module": row.get("module_name"),
        }
        if row.get("widget_name"):
            entry["widget"] = row.get("widget_name")
        if row.get("groups_attr"):
            entry["groups"] = row.get("groups_attr")
        if row.get("invisible_expr"):
            entry["invisible_when"] = row.get("invisible_expr")
        if row.get("attrs_expr"):
            entry["attrs"] = row.get("attrs_expr")
        if row.get("field_attrs"):
            entry["field_attrs"] = json_col(row, "field_attrs", {})
        view_level.append(entry)

    # Field group restrictions from field_groups_map
    fg_rows = await async_query(
        db_path,
        """
        SELECT group_xml_id, source, module_name
        FROM field_groups_map
        WHERE model_name = ? AND field_name = ?
        """,
        (model_name, field_name),
    )

    group_restrictions = [
        {"group": r["group_xml_id"], "source": r.get("source")} for r in fg_rows
    ]

    # If python_level groups not already covered
    if field_row.get("groups") and not any(
        g["group"] == field_row["groups"] for g in group_restrictions
    ):
        for g in (field_row["groups"] or "").split(","):
            g = g.strip()
            if g:
                group_restrictions.append({"group": g, "source": "field_def"})

    return {
        "model": model_name,
        "field": field_name,
        "field_type": field_row.get("field_type"),
        "label": field_row.get("string_label"),
        "python_level": python_level,
        "view_level": view_level,
        "group_restrictions": group_restrictions,
        "appears_in_views": len(view_level),
    }
