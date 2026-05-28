"""Tool 01: get_model_schema — full field/inherit schema for an Odoo model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one, json_col


def _field_row_to_obj(f: dict, inherited_from: str | None = None) -> dict:
    """Convert a fields DB row to a sparse field object (omits default/null values)."""
    field_obj: dict = {
        "name": f.get("field_name"),
        "type": f.get("field_type"),
        "module": f.get("module_name"),
    }
    if inherited_from:
        field_obj["inherited_from"] = inherited_from
    if f.get("string_label"):
        field_obj["string"] = f["string_label"]
    if f.get("required"):
        field_obj["required"] = True
    if f.get("readonly"):
        field_obj["readonly"] = True
    if not f.get("store", 1):
        field_obj["store"] = False  # non-default: explicitly not stored
    if f.get("compute"):
        field_obj["compute"] = f["compute"]
    if f.get("related"):
        field_obj["related"] = f["related"]
    if f.get("comodel_name"):
        field_obj["comodel"] = f["comodel_name"]
    if f.get("groups"):
        field_obj["groups"] = f["groups"]
    if f.get("help_text"):
        field_obj["help"] = f["help_text"]
    sel = json.loads(f.get("selection_values") or "[]")
    if sel:
        field_obj["selection"] = sel
    return field_obj


def _field_row_to_compact(f: dict, inherited_from: str | None = None) -> str:
    """Convert a fields DB row to a compact string representation."""
    parts = [f"{f.get('field_name')}: {f.get('field_type', '?')}"]
    comodel = f.get("comodel_name")
    if comodel:
        parts[0] += f"({comodel})"
    flags = []
    if inherited_from:
        flags.append(f"inherited_from:{inherited_from}")
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
    return " ".join(parts)


async def _collect_inherited_fields(
    db_path: Path,
    inherit_parents: str | list[str],
    visited: set[str],
    compact: bool,
) -> list:
    """Recursively collect fields from parent models (BFS), with inherited_from attribution.

    Args:
        db_path:         Path to the SQLite index.
        inherit_parents: Parent model name(s) from _inherit.
        visited:         Set of already-visited model names (prevents cycles).
        compact:         Whether to return compact strings or sparse dicts.
    """
    inherited: list = []

    parents = [inherit_parents] if isinstance(inherit_parents, str) else inherit_parents

    for parent_name in parents:
        if not parent_name or parent_name in visited:
            continue
        visited.add(parent_name)

        # Fetch parent's own fields
        parent_field_rows = await async_query(
            db_path,
            "SELECT * FROM fields WHERE model_name = ? ORDER BY field_name",
            (parent_name,),
        )
        for f in parent_field_rows:
            if compact:
                inherited.append(_field_row_to_compact(f, inherited_from=parent_name))
            else:
                inherited.append(_field_row_to_obj(f, inherited_from=parent_name))

        # Recurse: fetch parent's own inherit_model
        parent_model_rows = await async_query(
            db_path,
            "SELECT * FROM models WHERE name = ? ORDER BY inherit_type",
            (parent_name,),
        )
        if parent_model_rows:
            parent_primary = next(
                (r for r in parent_model_rows if r.get("inherit_type") == "primary"),
                parent_model_rows[0],
            )
            raw_parent_inherit = parent_primary.get("inherit_model")
            if raw_parent_inherit:
                if raw_parent_inherit.startswith("["):
                    try:
                        grandparent_inherit: str | list[str] = json.loads(raw_parent_inherit)
                    except Exception:
                        grandparent_inherit = raw_parent_inherit
                else:
                    grandparent_inherit = raw_parent_inherit
                deeper = await _collect_inherited_fields(db_path, grandparent_inherit, visited, compact)
                inherited.extend(deeper)

    return inherited


async def _collect_inherited_methods(
    db_path: Path,
    inherit_parents: str | list[str],
    visited: set[str],
) -> list[dict]:
    """Recursively collect methods from parent models (BFS), with inherited_from attribution.

    Returns a list of dicts: {name, module, decorators, inherited_from}.
    """
    inherited: list[dict] = []

    parents = [inherit_parents] if isinstance(inherit_parents, str) else inherit_parents

    for parent_name in parents:
        if not parent_name or parent_name in visited:
            continue
        visited.add(parent_name)

        parent_method_rows = await async_query(
            db_path,
            "SELECT method_name, decorator_types, module_name FROM methods "
            "WHERE model_name = ? ORDER BY method_name",
            (parent_name,),
        )
        for r in parent_method_rows:
            inherited.append(
                {
                    "name": r["method_name"],
                    "module": r["module_name"],
                    "decorators": json_col(r, "decorator_types", []),
                    "inherited_from": parent_name,
                }
            )

        # Recurse up
        parent_model_rows = await async_query(
            db_path,
            "SELECT * FROM models WHERE name = ? ORDER BY inherit_type",
            (parent_name,),
        )
        if parent_model_rows:
            parent_primary = next(
                (r for r in parent_model_rows if r.get("inherit_type") == "primary"),
                parent_model_rows[0],
            )
            raw_parent_inherit = parent_primary.get("inherit_model")
            if raw_parent_inherit:
                if raw_parent_inherit.startswith("["):
                    try:
                        grandparent_inherit: str | list[str] = json.loads(raw_parent_inherit)
                    except Exception:
                        grandparent_inherit = raw_parent_inherit
                else:
                    grandparent_inherit = raw_parent_inherit
                deeper = await _collect_inherited_methods(db_path, grandparent_inherit, visited)
                inherited.extend(deeper)

    return inherited


async def get_model_schema(
    model_name: str,
    get_db: Callable[[], Path],
    compact: bool = False,
    fields_limit: int = 200,
    include_inherited: bool = True,
) -> dict:
    """Get complete schema for an Odoo model: fields, types, compute methods,
    inheritance chain, and related models.

    Args:
        model_name:        Odoo model technical name (e.g. 'sale.order').
        get_db:            Callable returning the SQLite db Path.
        compact:           When True, collapse each field to a single descriptive
                           string instead of a full object.  ~10× fewer tokens;
                           ideal for an AI quick-scan before drilling in.
        fields_limit:      Maximum number of fields to return (default 200).
                           Increase when you need the full list on very large models.
        include_inherited: When True (default), resolve _inherit parents transitively
                           and append their fields with an 'inherited_from' key.
                           Pass False to see only fields defined directly on this model.
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

    # Fetch own fields (honour fields_limit)
    field_rows = await async_query(
        db_path,
        "SELECT * FROM fields WHERE model_name = ? ORDER BY field_name LIMIT ?",
        (model_name, fields_limit),
    )
    total_fields_own = await async_query_one(
        db_path,
        "SELECT COUNT(*) AS cnt FROM fields WHERE model_name = ?",
        (model_name,),
    )
    total_fields_count: int = (total_fields_own or {}).get("cnt", len(field_rows))

    if compact:
        # One compact string per field: "name: Type[(comodel)] [flags]"
        fields: list = [_field_row_to_compact(f) for f in field_rows]
    else:
        fields = [_field_row_to_obj(f) for f in field_rows]

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

    # inherit_model may be a JSON list (when _name + _inherit=[list]) or a plain string
    raw_inherit_model = primary_row.get("inherit_model")
    if raw_inherit_model and raw_inherit_model.startswith("["):
        try:
            inherit_parents: list | str | None = json.loads(raw_inherit_model)
        except Exception:
            inherit_parents = raw_inherit_model
    else:
        inherit_parents = raw_inherit_model

    # Transitively resolve parent fields when requested
    if include_inherited and inherit_parents:
        visited: set[str] = {model_name}
        inherited_fields = await _collect_inherited_fields(db_path, inherit_parents, visited, compact)

        # Deduplicate: own field names always take priority over inherited ones
        if compact:
            own_names = {s.split(":")[0].strip() for s in fields}
            for ifield in inherited_fields:
                fname = ifield.split(":")[0].strip()
                if fname not in own_names:
                    fields.append(ifield)
        else:
            own_names = {fo["name"] for fo in fields}
            for ifield in inherited_fields:
                if ifield["name"] not in own_names:
                    fields.append(ifield)

    # -----------------------------------------------------------------------
    # Methods: own methods defined on this model (compact list)
    # Plus inherited methods from parent chain with inherited_from attribution.
    # This lets the AI know what methods are callable/overrideable on the model.
    # -----------------------------------------------------------------------
    own_method_rows = await async_query(
        db_path,
        "SELECT method_name, decorator_types, module_name FROM methods "
        "WHERE model_name = ? ORDER BY method_name",
        (model_name,),
    )
    methods_list: list[dict] = [
        {
            "name": r["method_name"],
            "module": r["module_name"],
            "decorators": json_col(r, "decorator_types", []),
        }
        for r in own_method_rows
    ]

    if include_inherited and inherit_parents:
        visited_m: set[str] = {model_name}
        inherited_methods = await _collect_inherited_methods(db_path, inherit_parents, visited_m)
        own_method_names = {m["name"] for m in methods_list}
        for im in inherited_methods:
            if im["name"] not in own_method_names:
                methods_list.append(im)
            # If same name exists in own methods, flag the own method as an override
            else:
                for om in methods_list:
                    if om["name"] == im["name"] and "module" in om:
                        om.setdefault("overrides", []).append(
                            {"model": im["inherited_from"], "module": im["module"]}
                        )

    result = {
        "model": model_name,
        "class_name": primary_row.get("python_class"),
        "description": primary_row.get("description"),
        "inherit_type": primary_row.get("inherit_type"),
        "inherit_model": inherit_parents,  # str, list[str], or None
        "inherits_map": json_col(primary_row, "inherits_map", {}),
        "is_abstract": bool(primary_row.get("abstract", 0)),
        "is_transient": bool(primary_row.get("transient", 0)),
        "module": primary_row.get("module_name"),
        "file": primary_row.get("file_path"),
        "line": primary_row.get("line_number"),
        "fields": fields,
        "methods": methods_list,
        "extensions": extensions,
        "total_fields": total_fields_count,
        "compact": compact,
        "include_inherited": include_inherited,
    }

    if total_fields_count > fields_limit:
        result["truncated"] = True
        result["truncated_note"] = (
            f"Showing {fields_limit}/{total_fields_count} own fields. "
            f"Pass fields_limit={total_fields_count} to see all."
        )

    return result
