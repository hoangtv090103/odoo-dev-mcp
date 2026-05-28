"""Tool 11: trace_button_to_method — trace a view button to its Python method."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def trace_button_to_method(
    view_xml_id: str,
    button_name: str,
    get_db: Callable[[], Path],
) -> dict:
    """Trace a button in a view to the Python method it calls and what that method does."""
    db_path = get_db()

    # Find button in view_element_refs
    btn_row = await async_query_one(
        db_path,
        """
        SELECT ver.*, v.model AS view_model, v.view_type
        FROM view_element_refs ver
        JOIN views v ON v.xml_id = ver.view_xml_id
        WHERE ver.view_xml_id = ?
          AND ver.element_type = 'button'
          AND (ver.button_name = ? OR ver.button_action = ?)
        LIMIT 1
        """,
        (view_xml_id, button_name, button_name),
    )

    if not btn_row:
        # Try without the view constraint (maybe wrong xml_id format)
        btn_row = await async_query_one(
            db_path,
            """
            SELECT ver.*, v.model AS view_model, v.view_type
            FROM view_element_refs ver
            JOIN views v ON v.xml_id = ver.view_xml_id
            WHERE ver.view_xml_id LIKE ?
              AND ver.element_type = 'button'
              AND (ver.button_name = ? OR ver.button_action = ?)
            LIMIT 1
            """,
            (f"%{view_xml_id}%", button_name, button_name),
        )

    if not btn_row:
        return {
            "error": f"Button '{button_name}' not found in view '{view_xml_id}'.",
            "view_xml_id": view_xml_id,
            "button_name": button_name,
        }

    model_name = btn_row.get("view_model")
    button_type = btn_row.get("button_type", "object")
    button_action = btn_row.get("button_action")
    actual_button_name = btn_row.get("button_name") or button_name

    result: dict = {
        "view_xml_id": btn_row.get("view_xml_id"),
        "view_type": btn_row.get("view_type"),
        "model": model_name,
        "button_name": actual_button_name,
        "button_type": button_type,
        "button_states": btn_row.get("button_states"),
        "button_groups": btn_row.get("button_groups"),
        "confirm_message": btn_row.get("button_confirm"),
        "invisible_when": btn_row.get("invisible_expr"),
    }

    if button_type == "object":
        # Resolve to Python method
        method_name = actual_button_name
        method_row = await async_query_one(
            db_path,
            """
            SELECT m.*, GROUP_CONCAT(dd.decorator_type, ',') AS dec_types
            FROM methods m
            LEFT JOIN decorators_detail dd ON dd.model_name = m.model_name
              AND dd.method_name = m.method_name
            WHERE m.model_name = ? AND m.method_name = ?
            GROUP BY m.id
            ORDER BY m.module_name
            LIMIT 1
            """,
            (model_name, method_name),
        )

        if method_row:
            result["resolved_method"] = {
                "method": method_name,
                "file": method_row.get("file_path"),
                "line": method_row.get("line_number"),
                "decorators": json_col(method_row, "decorator_types", []),
                "state_transitions": json_col(method_row, "state_transitions", []),
                "calls_models": json_col(method_row, "calls_models", []),
                "raises_validation": bool(method_row.get("raises_validation", 0)),
                "is_cron_target": bool(method_row.get("is_cron_target", 0)),
            }
        else:
            result["resolved_method"] = {
                "method": method_name,
                "note": "Method not found in index (may be inherited or dynamic).",
            }

    elif button_type == "action":
        # Resolve to action
        action_ref = button_action or actual_button_name
        action_row = await async_query_one(
            db_path,
            """
            SELECT xml_id, action_type, name, res_model, view_mode,
                   server_method, binding_model, tag, module_name
            FROM actions
            WHERE xml_id = ? OR xml_id LIKE ?
            LIMIT 1
            """,
            (action_ref, f"%.{action_ref}"),
        )

        if action_row:
            result["resolved_action"] = {
                "xml_id": action_row.get("xml_id"),
                "type": action_row.get("action_type"),
                "name": action_row.get("name"),
                "res_model": action_row.get("res_model"),
                "view_mode": action_row.get("view_mode"),
                "server_method": action_row.get("server_method"),
                "module": action_row.get("module_name"),
            }
        else:
            result["resolved_action"] = {
                "action_ref": action_ref,
                "note": "Action not found in index.",
            }

    elif button_type == "url":
        result["url"] = button_action

    return result
