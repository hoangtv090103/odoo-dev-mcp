"""Tool 04: get_method_logic — decorators, state transitions, ORM calls for a method."""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Callable

from ..db.connection import AsyncConn, json_col


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_inherit_model(raw: str | None) -> list[str]:
    """Normalise inherit_model (plain string or JSON list) → list of parent names."""
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return _json.loads(raw)
        except Exception:
            return [raw]
    return [raw]


async def _resolve_method_row(
    conn: AsyncConn,
    model_name: str,
    method_name: str,
) -> tuple[dict | None, str | None]:
    """Return (method_row, resolved_model).

    First checks model_name directly.  If not found, walks the ancestor
    chain (BFS via inherit_model) and returns the first ancestor that
    defines the method.

    resolved_model is None when the method is defined directly on model_name,
    or the ancestor model name when found via inheritance.
    """
    # Direct lookup first
    row = await conn.query_one(
        "SELECT * FROM methods WHERE model_name = ? AND method_name = ? "
        "ORDER BY module_name LIMIT 1",
        (model_name, method_name),
    )
    if row:
        return row, None

    # BFS through ancestor chain
    inherit_row = await conn.query_one(
        "SELECT inherit_model FROM models WHERE name = ? AND inherit_type = 'primary'",
        (model_name,),
    )
    if not inherit_row:
        return None, None

    visited: set[str] = {model_name}
    queue: list[str] = _parse_inherit_model(inherit_row.get("inherit_model"))

    while queue:
        ancestor = queue.pop(0)
        if not ancestor or ancestor in visited:
            continue
        visited.add(ancestor)

        row = await conn.query_one(
            "SELECT * FROM methods WHERE model_name = ? AND method_name = ? "
            "ORDER BY module_name LIMIT 1",
            (ancestor, method_name),
        )
        if row:
            return row, ancestor  # found via inheritance

        # Continue up
        gp_row = await conn.query_one(
            "SELECT inherit_model FROM models WHERE name = ? AND inherit_type = 'primary'",
            (ancestor,),
        )
        if gp_row:
            queue.extend(_parse_inherit_model(gp_row.get("inherit_model")))

    return None, None


async def _build_decorators(
    conn: AsyncConn,
    model_name: str,
    method_name: str,
    method_row: dict,
) -> tuple[list[str], str | None, list | None]:
    """Return (decorators, api_returns_model, ormcache_keys)."""
    decorator_rows = await conn.query(
        """
        SELECT decorator_type, depends_fields, depends_ctx_keys,
               constrains_fields, onchange_fields, returns_model,
               ormcache_keys, file_path, line_number
        FROM decorators_detail
        WHERE model_name = ? AND method_name = ?
        """,
        (model_name, method_name),
    )

    decorators: list[str] = []
    api_returns_model: str | None = method_row.get("api_returns_model")
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

    # Pull simple decorators from methods.decorator_types not already captured
    for d in json_col(method_row, "decorator_types", []):
        if d and not any(d in dec for dec in decorators):
            decorators.append(f"@{d}")

    return decorators, api_returns_model, ormcache_keys


def _detect_calls_super(method_row: dict) -> bool:
    """Scan method body in source file for super() calls."""
    file_path = method_row.get("file_path")
    body_start = method_row.get("body_start_line")
    body_end = method_row.get("body_end_line")
    if not (file_path and body_start and body_end):
        return False
    try:
        src_lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        body_text = "\n".join(src_lines[body_start - 1 : body_end])
        return "super()" in body_text
    except Exception:
        return False


async def _detect_override(
    conn: AsyncConn,
    model_name: str,
    method_name: str,
    include_source: bool,
) -> tuple[bool, list[dict]]:
    """Return (is_override, overrides_from) by walking the ancestor chain."""
    inherit_row = await conn.query_one(
        "SELECT inherit_model FROM models WHERE name = ? AND inherit_type = 'primary'",
        (model_name,),
    )
    if not inherit_row:
        return False, []

    initial_parents = _parse_inherit_model(inherit_row.get("inherit_model"))
    if not initial_parents:
        return False, []

    is_override = False
    overrides_from: list[dict] = []
    visited: set[str] = {model_name}
    queue: list[str] = list(initial_parents)

    while queue:
        ancestor = queue.pop(0)
        if not ancestor or ancestor in visited:
            continue
        visited.add(ancestor)

        ancestor_method = await conn.query_one(
            "SELECT module_name, file_path, line_number FROM methods "
            "WHERE model_name = ? AND method_name = ?",
            (ancestor, method_name),
        )
        if ancestor_method:
            is_override = True
            entry: dict = {
                "model": ancestor,
                "module": ancestor_method.get("module_name"),
            }
            if include_source:
                entry["file"] = ancestor_method.get("file_path")
                entry["line"] = ancestor_method.get("line_number")
            overrides_from.append(entry)

        gp_row = await conn.query_one(
            "SELECT inherit_model FROM models WHERE name = ? AND inherit_type = 'primary'",
            (ancestor,),
        )
        if gp_row:
            queue.extend(_parse_inherit_model(gp_row.get("inherit_model")))

    return is_override, overrides_from


# ── Public tool ───────────────────────────────────────────────────────────────

async def get_method_logic(
    model_name: str,
    method_name: str,
    get_db: Callable[[], Path],
    include_source: bool = True,
) -> dict:
    """Get what a Python method does: decorators, state transitions it causes,
    ORM calls it makes, constraints it enforces, and whether it overrides a parent.

    Automatically resolves inherited methods: if the method is not defined
    directly on model_name but exists on an ancestor, it is returned with
    ``is_inherited=True`` and ``resolved_on`` set to the ancestor model name.

    Args:
        model_name:     Odoo model technical name (e.g. 'sale.order').
        method_name:    Python method name (e.g. 'action_confirm').
        get_db:         Callable returning the SQLite db Path.
        include_source: When True (default), include file path and line number
                        so the AI can read the source directly.
    """
    db_path = get_db()

    async with AsyncConn(db_path) as conn:
        # ── 1. Resolve method row (direct or via inheritance) ─────────────────
        method_row, resolved_on = await _resolve_method_row(conn, model_name, method_name)

        if not method_row:
            return {
                "error": f"Method '{method_name}' not found on model '{model_name}' "
                         "or any of its ancestors.",
                "model": model_name,
                "method": method_name,
            }

        # The model that actually defines this method (may differ from requested)
        defining_model = resolved_on or model_name
        is_inherited = resolved_on is not None

        # ── 2. Decorator details (looked up on the defining model) ────────────
        decorators, api_returns_model, ormcache_keys = await _build_decorators(
            conn, defining_model, method_name, method_row
        )

        # ── 3. Cron target check ──────────────────────────────────────────────
        cron_row = await conn.query_one(
            "SELECT xml_id FROM cron_jobs WHERE model_name = ? AND method_name = ?",
            (defining_model, method_name),
        )

        # ── 4. super() detection ──────────────────────────────────────────────
        calls_super = _detect_calls_super(method_row)

        # ── 5. Override detection ─────────────────────────────────────────────
        # Only meaningful when the method is defined directly on the requested model.
        # When it's inherited (is_inherited=True), there is no override.
        if is_inherited:
            is_override = False
            overrides_from: list[dict] = []
        else:
            is_override, overrides_from = await _detect_override(
                conn, model_name, method_name, include_source
            )

    # ── Build result ──────────────────────────────────────────────────────────
    result: dict = {
        "model": model_name,
        "method": method_name,
        "module": method_row.get("module_name"),
        "decorators": decorators,
        "state_transitions": json_col(method_row, "state_transitions", []),
        "calls_models": json_col(method_row, "calls_models", []),
        "raises_validation": bool(method_row.get("raises_validation", 0)),
        "is_cron_target": bool(cron_row),
        "ormcache_keys": ormcache_keys,
        "api_returns_model": api_returns_model,
        "calls_super": calls_super,
        # Override / inheritance info
        "is_override": is_override,
        "overrides_from": overrides_from,
        # Common Odoo bug: overriding without calling super() silently breaks
        # other addons in the MRO chain.
        "missing_super_call": is_override and not calls_super,
        # Inheritance resolution info
        "is_inherited": is_inherited,
        "resolved_on": resolved_on,  # None = defined directly on model_name
    }

    if include_source:
        result["file"] = method_row.get("file_path")
        result["line"] = method_row.get("line_number")

    return result
