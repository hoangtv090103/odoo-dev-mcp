"""Tool 05: get_state_machine — full state machine for an Odoo model."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def get_state_machine(model_name: str, get_db: Callable[[], Path]) -> dict:
    """Get the complete state machine for a model: all states, transitions,
    and the buttons/methods that trigger them."""
    db_path = get_db()

    sm_row = await async_query_one(
        db_path,
        "SELECT * FROM state_machines WHERE model_name = ?",
        (model_name,),
    )

    if not sm_row:
        # Attempt to find a state-like Selection field to give a hint
        state_field = await async_query_one(
            db_path,
            """
            SELECT field_name, selection_values FROM fields
            WHERE model_name = ? AND field_name = 'state'
            LIMIT 1
            """,
            (model_name,),
        )
        if state_field:
            selections = json_col(state_field, "selection_values", [])
            return {
                "model": model_name,
                "state_field": "state",
                "states": selections,
                "transitions": [],
                "total_transitions": 0,
                "note": "State field found but no transition data was indexed.",
            }
        return {
            "error": f"No state machine found for model '{model_name}'.",
            "model": model_name,
        }

    states = json_col(sm_row, "states", [])
    raw_transitions = json_col(sm_row, "transitions", [])
    state_field = sm_row.get("field_name", "state")

    # Enrich transitions with view button information
    enriched_transitions = []
    for t in raw_transitions:
        from_state = t.get("from") or t.get("from_state")
        to_state = t.get("to") or t.get("to_state")
        method = t.get("method")
        button = t.get("button") or method

        # Find which views contain a button that calls this method
        button_view = None
        if button:
            btn_row = await async_query_one(
                db_path,
                """
                SELECT ver.view_xml_id
                FROM view_element_refs ver
                JOIN views v ON v.xml_id = ver.view_xml_id
                WHERE ver.element_type = 'button'
                  AND (ver.button_name = ? OR ver.button_action = ?)
                  AND v.model = ?
                LIMIT 1
                """,
                (button, button, model_name),
            )
            if btn_row:
                button_view = btn_row["view_xml_id"]

        enriched_transitions.append(
            {
                "from_state": from_state,
                "to_state": to_state,
                "method": method,
                "button": button,
                "button_view": button_view,
            }
        )

    return {
        "model": model_name,
        "state_field": state_field,
        "states": states,
        "transitions": enriched_transitions,
        "total_transitions": len(enriched_transitions),
    }
