"""Tool 04: get_method_logic — decorators, state transitions, ORM calls for a method."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


async def get_method_logic(
    model_name: str,
    method_name: str,
    get_db: Callable[[], Path],
    include_source: bool = True,
) -> dict:
    """Get what a Python method does: decorators, state transitions it causes,
    ORM calls it makes, and constraints it enforces.

    Args:
        model_name:     Odoo model technical name (e.g. 'sale.order').
        method_name:    Python method name (e.g. 'action_confirm').
        get_db:         Callable returning the SQLite db Path.
        include_source: When True (default), include file path and line number
                        so the AI can read the source directly.
                        Pass False to get a more compact summary without
                        file-location metadata.
    """
    db_path = get_db()

    method_row = await async_query_one(
        db_path,
        """
        SELECT * FROM methods
        WHERE model_name = ? AND method_name = ?
        ORDER BY module_name
        LIMIT 1
        """,
        (model_name, method_name),
    )

    if not method_row:
        return {
            "error": f"Method '{method_name}' not found on model '{model_name}'.",
            "model": model_name,
            "method": method_name,
        }

    # Decorator details
    decorator_rows = await async_query(
        db_path,
        """
        SELECT decorator_type, depends_fields, depends_ctx_keys,
               constrains_fields, onchange_fields, returns_model,
               ormcache_keys, file_path, line_number
        FROM decorators_detail
        WHERE model_name = ? AND method_name = ?
        """,
        (model_name, method_name),
    )

    # Build human-readable decorator list
    decorators = []
    api_returns_model = method_row.get("api_returns_model")
    ormcache_keys = json_col(method_row, "ormcache_keys")
    for row in decorator_rows:
        dtype = row.get("decorator_type", "")
        if dtype == "api.depends":
            fields = json_col(row, "depends_fields", [])
            decorators.append(f"@api.depends({', '.join(repr(f) for f in fields)})")
        elif dtype == "api.depends_context":
            keys = json_col(row, "depends_ctx_keys", [])
            decorators.append(f"@api.depends_context({', '.join(repr(k) for k in keys)})")
        elif dtype == "api.constrains":
            fields = json_col(row, "constrains_fields", [])
            decorators.append(f"@api.constrains({', '.join(repr(f) for f in fields)})")
        elif dtype == "api.onchange":
            fields = json_col(row, "onchange_fields", [])
            decorators.append(f"@api.onchange({', '.join(repr(f) for f in fields)})")
        elif dtype == "api.returns":
            model_ref = row.get("returns_model") or ""
            decorators.append(f"@api.returns({model_ref!r})")
            if model_ref:
                api_returns_model = model_ref
        elif dtype.startswith("tools.ormcache"):
            keys = json_col(row, "ormcache_keys", [])
            decorators.append(f"@{dtype}({', '.join(repr(k) for k in keys)})")
            ormcache_keys = keys
        else:
            decorators.append(f"@{dtype}")

    # Also pull from methods.decorator_types for simple decorators not in detail table
    simple_decs = json_col(method_row, "decorator_types", [])
    for d in simple_decs:
        if d and not any(d in dec for dec in decorators):
            decorators.append(f"@{d}")

    # Check if used as cron target
    cron_row = await async_query_one(
        db_path,
        "SELECT xml_id FROM cron_jobs WHERE model_name = ? AND method_name = ?",
        (model_name, method_name),
    )

    # Detect super() calls by scanning the method body in the source file
    calls_super = False
    file_path = method_row.get("file_path")
    body_start = method_row.get("body_start_line")
    body_end = method_row.get("body_end_line")
    if file_path and body_start and body_end:
        try:
            from pathlib import Path as _Path
            src_lines = _Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
            # body_start_line / body_end_line are 1-based
            body_text = "\n".join(src_lines[body_start - 1 : body_end])
            calls_super = "super()" in body_text
        except Exception:
            pass

    result = {
        "model": model_name,
        "method": method_name,
        "decorators": decorators,
        "state_transitions": json_col(method_row, "state_transitions", []),
        "calls_models": json_col(method_row, "calls_models", []),
        "raises_validation": bool(method_row.get("raises_validation", 0)),
        "is_cron_target": bool(cron_row),
        "ormcache_keys": ormcache_keys,
        "api_returns_model": api_returns_model,
        "calls_super": calls_super,
    }

    if include_source:
        result["file"] = method_row.get("file_path")
        result["line"] = method_row.get("line_number")

    return result
