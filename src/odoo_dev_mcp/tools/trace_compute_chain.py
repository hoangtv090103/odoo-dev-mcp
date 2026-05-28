"""Tool 08: trace_compute_chain — trace how a computed field is calculated."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def trace_compute_chain(
    model_name: str,
    field_name: str,
    get_db: Callable[[], Path],
) -> dict:
    """Trace how a computed field is calculated: its compute method,
    @api.depends fields, and @api.depends_context keys."""
    db_path = get_db()

    field_row = await async_query_one(
        db_path,
        """
        SELECT * FROM fields
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

    compute_method = field_row.get("compute")
    related = field_row.get("related")
    inverse_method = field_row.get("inverse")
    search_method = field_row.get("search")

    # If it's a related field, not compute
    if not compute_method and related:
        return {
            "model": model_name,
            "field": field_name,
            "field_type": field_row.get("field_type"),
            "is_stored": bool(field_row.get("store", 1)),
            "compute_method": None,
            "depends_fields": [],
            "depends_context": [],
            "inverse_method": inverse_method,
            "search_method": search_method,
            "related_chain": related,
            "compute_file": field_row.get("file_path"),
            "compute_line": field_row.get("line_number"),
            "state_transitions": [],
            "orm_calls": [],
        }

    if not compute_method:
        return {
            "model": model_name,
            "field": field_name,
            "field_type": field_row.get("field_type"),
            "is_stored": bool(field_row.get("store", 1)),
            "compute_method": None,
            "depends_fields": json_col(field_row, "depends", []),
            "depends_context": [],
            "inverse_method": inverse_method,
            "search_method": search_method,
            "related_chain": related,
            "compute_file": field_row.get("file_path"),
            "compute_line": field_row.get("line_number"),
            "state_transitions": [],
            "orm_calls": [],
            "note": "Field has no compute method.",
        }

    # Fetch @api.depends detail for the compute method
    depends_rows = await async_query(
        db_path,
        """
        SELECT depends_fields, depends_ctx_keys
        FROM decorators_detail
        WHERE model_name = ? AND method_name = ?
          AND decorator_type IN ('api.depends', 'api.depends_context')
        """,
        (model_name, compute_method),
    )

    depends_fields: list[str] = []
    depends_context: list[str] = []
    for row in depends_rows:
        df = json_col(row, "depends_fields", [])
        dc = json_col(row, "depends_ctx_keys", [])
        depends_fields.extend(df)
        depends_context.extend(dc)

    # Fallback: use fields.depends column
    if not depends_fields:
        depends_fields = json_col(field_row, "depends", [])

    # Fetch method details for state transitions and ORM calls
    method_row = await async_query_one(
        db_path,
        """
        SELECT file_path, line_number, state_transitions, calls_models
        FROM methods
        WHERE model_name = ? AND method_name = ?
        ORDER BY module_name
        LIMIT 1
        """,
        (model_name, compute_method),
    )

    compute_file = field_row.get("file_path")
    compute_line = field_row.get("line_number")
    state_transitions = []
    orm_calls = []

    if method_row:
        compute_file = method_row.get("file_path") or compute_file
        compute_line = method_row.get("line_number") or compute_line
        state_transitions = json_col(method_row, "state_transitions", [])
        orm_calls = json_col(method_row, "calls_models", [])

    return {
        "model": model_name,
        "field": field_name,
        "field_type": field_row.get("field_type"),
        "is_stored": bool(field_row.get("store", 1)),
        "compute_method": compute_method,
        "depends_fields": depends_fields,
        "depends_context": depends_context,
        "inverse_method": inverse_method,
        "search_method": search_method,
        "related_chain": related,
        "compute_file": compute_file,
        "compute_line": compute_line,
        "state_transitions": state_transitions,
        "orm_calls": orm_calls,
    }
