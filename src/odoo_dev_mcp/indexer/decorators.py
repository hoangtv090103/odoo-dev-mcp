"""
Decorator & field attribute detail.

For each method already in the methods table (populated by the structural step),
this step:
  - Inserts rows into decorators_detail
  - Updates methods.decorator_types, api_returns_model, ormcache_keys
  - Inserts context_dependencies for @api.depends_context
  - Inserts selection_extensions for fields with selection_add
  - Inserts field_groups_map for fields with groups=
  - Indexes HTTP routes from controller @http.route / @route methods
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

from ..parsers.python_parser import (
    DecoratorInfo,
    MethodInfo,
    OdooModelInfo,
    ControllerInfo,
    parse_python_file,
    unquote,
)
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── Path param extraction ──────────────────────────────────────────────────────

_PATH_PARAM_RE = re.compile(r"<(?:\w+:)?(\w+)>")


def _extract_path_params(route_pattern: str) -> list[str]:
    """Extract path parameter names from a route pattern like /order/<int:order_id>."""
    return _PATH_PARAM_RE.findall(route_pattern)


# ── Route pattern normalisation ───────────────────────────────────────────────

def _normalise_route(raw: str) -> tuple[str, list[str]]:
    """
    Given a raw route arg (string literal or list literal),
    return (primary_pattern, all_patterns).
    """
    from ..parsers.python_parser import unquote
    # Try as a simple string literal
    single = unquote(raw)
    if single is not None:
        return single, [single]
    # Try as a list literal
    if raw.strip().startswith("["):
        try:
            import ast
            val = ast.literal_eval(raw.strip())
            if isinstance(val, (list, tuple)):
                patterns = [str(v) for v in val]
                return patterns[0] if patterns else raw, patterns
        except Exception:
            pass
    return raw, [raw]


# ── Decorator detail helpers ──────────────────────────────────────────────────

def _parse_decorator_detail(
    dec: DecoratorInfo,
    model_name: str,
    method_name: str,
    file_path: str,
    line_number: int,
) -> Optional[dict]:
    """
    Convert a DecoratorInfo into a dict ready for decorators_detail insertion.
    Returns None if the decorator is not interesting.
    """
    name = dec.name
    args = dec.args
    kwargs = dec.kwargs

    base: dict = {
        "model_name": model_name,
        "method_name": method_name,
        "decorator_type": name,
        "file_path": file_path,
        "line_number": line_number,
        "depends_fields": None,
        "depends_ctx_keys": None,
        "constrains_fields": None,
        "onchange_fields": None,
        "ondelete_at_unlink": None,
        "returns_model": None,
        "ormcache_keys": None,
        "http_route": None,
        "http_auth": None,
        "http_type": None,
        "http_methods": None,
        "test_tags": None,
    }

    if name in ("api.depends",):
        fields = [unquote(a) or a for a in args]
        base["depends_fields"] = json.dumps(fields)

    elif name in ("api.depends_context",):
        keys = [unquote(a) or a for a in args]
        base["depends_ctx_keys"] = json.dumps(keys)

    elif name in ("api.constrains",):
        fields = [unquote(a) or a for a in args]
        base["constrains_fields"] = json.dumps(fields)

    elif name in ("api.onchange",):
        fields = [unquote(a) or a for a in args]
        base["onchange_fields"] = json.dumps(fields)

    elif name in ("api.ondelete",):
        at_unlink_raw = kwargs.get("at_unlink", "False")
        base["ondelete_at_unlink"] = 1 if at_unlink_raw.lower() == "true" else 0

    elif name in ("api.returns",):
        if args:
            returns_model = unquote(args[0]) or args[0]
            base["returns_model"] = returns_model

    elif name in ("api.model", "api.model_create_multi", "api.autovacuum"):
        pass  # just record the decorator type

    elif name in ("ormcache", "tools.ormcache", "api.ormcache"):
        keys = [unquote(a) or a for a in args]
        base["ormcache_keys"] = json.dumps(keys)

    elif name in ("http.route", "route"):
        if args:
            primary, patterns = _normalise_route(args[0])
            base["http_route"] = primary
        auth = unquote(kwargs.get("auth", "")) or kwargs.get("auth")
        route_type = unquote(kwargs.get("type", "")) or kwargs.get("type")
        methods_raw = kwargs.get("methods")
        if methods_raw:
            try:
                import ast
                methods_val = ast.literal_eval(methods_raw)
                if isinstance(methods_val, (list, tuple)):
                    base["http_methods"] = json.dumps(list(methods_val))
                else:
                    base["http_methods"] = json.dumps([str(methods_val)])
            except Exception:
                base["http_methods"] = json.dumps([methods_raw])
        base["http_auth"] = auth
        base["http_type"] = route_type

    else:
        # Not a known interesting decorator — still store it
        pass

    return base


# ── Insert helpers ────────────────────────────────────────────────────────────

def _insert_decorator_detail(conn: sqlite3.Connection, detail: dict) -> None:
    conn.execute(
        """
        INSERT INTO decorators_detail
            (model_name, method_name, decorator_type,
             depends_fields, depends_ctx_keys, constrains_fields, onchange_fields,
             ondelete_at_unlink, returns_model, ormcache_keys,
             http_route, http_auth, http_type, http_methods, test_tags,
             file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            detail["model_name"],
            detail["method_name"],
            detail["decorator_type"],
            detail["depends_fields"],
            detail["depends_ctx_keys"],
            detail["constrains_fields"],
            detail["onchange_fields"],
            detail["ondelete_at_unlink"],
            detail["returns_model"],
            detail["ormcache_keys"],
            detail["http_route"],
            detail["http_auth"],
            detail["http_type"],
            detail["http_methods"],
            detail["test_tags"],
            detail["file_path"],
            detail["line_number"],
        ),
    )


def _update_method(
    conn: sqlite3.Connection,
    model_name: str,
    method_name: str,
    module_name: str,
    decorator_types: list[str],
    api_returns_model: Optional[str],
    ormcache_keys: Optional[str],
) -> None:
    conn.execute(
        """
        UPDATE methods
        SET decorator_types = ?,
            api_returns_model = COALESCE(?, api_returns_model),
            ormcache_keys = COALESCE(?, ormcache_keys)
        WHERE model_name = ? AND method_name = ? AND module_name = ?
        """,
        (
            json.dumps(decorator_types),
            api_returns_model,
            ormcache_keys,
            model_name,
            method_name,
            module_name,
        ),
    )


def _insert_context_dep(
    conn: sqlite3.Connection,
    model_name: str,
    method_name: str,
    ctx_keys: list[str],
    module_name: str,
    file_path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO context_dependencies
            (model_name, method_name, context_keys, module_name, file_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (model_name, method_name, json.dumps(ctx_keys), module_name, file_path),
    )


def _insert_http_route(
    conn: sqlite3.Connection,
    dec: DecoratorInfo,
    method: MethodInfo,
    controller_class: str,
    module_name: str,
    file_path: str,
) -> None:
    """Insert a row into http_routes from a @http.route decorator."""
    args = dec.args
    kwargs = dec.kwargs

    route_pattern = ""
    route_patterns_json = "[]"
    if args:
        primary, patterns = _normalise_route(args[0])
        route_pattern = primary
        route_patterns_json = json.dumps(patterns)

    if not route_pattern:
        return

    auth = unquote(kwargs.get("auth", "")) or kwargs.get("auth") or "user"
    route_type = unquote(kwargs.get("type", "")) or kwargs.get("type") or "http"

    methods_raw = kwargs.get("methods")
    if methods_raw:
        try:
            import ast
            mv = ast.literal_eval(methods_raw)
            http_methods_json = json.dumps(list(mv)) if isinstance(mv, (list, tuple)) else json.dumps([str(mv)])
        except Exception:
            http_methods_json = json.dumps(["GET", "POST"])
    else:
        http_methods_json = json.dumps(["GET", "POST"])

    website_raw = kwargs.get("website", "False")
    website = 1 if website_raw.lower() in ("true", "1") else 0

    sitemap_raw = kwargs.get("sitemap", "False")
    sitemap = 1 if sitemap_raw.lower() in ("true", "1") else 0

    cors = unquote(kwargs.get("cors", "")) or kwargs.get("cors")

    csrf_raw = kwargs.get("csrf", "True")
    csrf = 0 if csrf_raw.lower() in ("false", "0") else 1

    path_params = json.dumps(_extract_path_params(route_pattern))

    conn.execute(
        """
        INSERT OR IGNORE INTO http_routes
            (route_pattern, route_patterns, auth, route_type, http_methods,
             website, sitemap, cors, csrf, controller_class, method_name,
             module_name, file_path, line_number, path_params)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            route_pattern,
            route_patterns_json,
            auth,
            route_type,
            http_methods_json,
            website,
            sitemap,
            cors,
            csrf,
            controller_class,
            method.name,
            module_name,
            file_path,
            method.line_number,
            path_params,
        ),
    )


def _insert_selection_extension(
    conn: sqlite3.Connection,
    model_name: str,
    field_name: str,
    selection_add_json: str,
    module_name: str,
    file_path: str,
    line_number: int,
) -> None:
    conn.execute(
        """
        INSERT INTO selection_extensions
            (model_name, field_name, added_values, defined_in_module, file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (model_name, field_name, selection_add_json, module_name, file_path, line_number),
    )


def _insert_field_groups(
    conn: sqlite3.Connection,
    model_name: str,
    field_name: str,
    groups_str: str,
    module_name: str,
) -> None:
    for group in [g.strip() for g in groups_str.split(",") if g.strip()]:
        conn.execute(
            """
            INSERT OR IGNORE INTO field_groups_map
                (model_name, field_name, group_xml_id, source, module_name)
            VALUES (?, ?, ?, 'field_def', ?)
            """,
            (model_name, field_name, group, module_name),
        )


# ── Entry point ────────────────────────────────────────────────────

def run_decorators(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 2: Decorator & field attribute detail.

    Iterates over all modules' Python files, re-parses them, and:
      - Inserts decorators_detail rows
      - Updates methods table with returns/ormcache info
      - Inserts context_dependencies
      - Inserts selection_extensions (fields with selection_add)
      - Inserts field_groups_map (fields with groups=)
      - Inserts http_routes from @route decorated controller methods
    """
    logger.info("Phase 2: decorator detail for %d modules", len(modules))

    for module in modules:
        for py_file in module.python_files:
            try:
                _process_file(conn, py_file, module.name)
            except Exception as exc:
                logger.warning("Phase2: error in %s: %s", py_file, exc)

    conn.commit()
    logger.info("Phase 2: complete")


def _process_file(
    conn: sqlite3.Connection,
    py_file: Path,
    module_name: str,
) -> None:
    """Process one Python file for Phase 2 data."""
    models, controllers = parse_python_file(py_file)
    file_path = str(py_file)

    for model in models:
        effective_name = model.model_name
        if not effective_name and isinstance(model.inherit, str):
            effective_name = model.inherit
        elif not effective_name and isinstance(model.inherit, list) and model.inherit:
            effective_name = model.inherit[0]
        if not effective_name:
            continue

        # Process fields for selection_add and groups
        for f in model.fields:
            kw = f.kwargs
            sel_add_raw = kw.get("selection_add")
            if sel_add_raw and sel_add_raw.startswith("["):
                try:
                    import ast
                    val = ast.literal_eval(sel_add_raw)
                    if isinstance(val, list):
                        _insert_selection_extension(
                            conn,
                            effective_name,
                            f.name,
                            json.dumps(val),
                            module_name,
                            file_path,
                            f.line_number,
                        )
                except Exception:
                    pass

            groups_raw = kw.get("groups")
            if groups_raw:
                groups_str = unquote(groups_raw) or groups_raw
                if groups_str:
                    _insert_field_groups(
                        conn, effective_name, f.name, groups_str, module_name
                    )

        # Process methods for decorator detail
        for method in model.methods:
            _process_method_decorators(
                conn, method, effective_name, module_name, file_path
            )

    # Process controller routes
    for ctrl in controllers:
        for method in ctrl.route_methods:
            for dec in method.decorators:
                if dec.name in ("http.route", "route"):
                    try:
                        _insert_http_route(
                            conn, dec, method, ctrl.class_name, module_name, file_path
                        )
                    except Exception as exc:
                        logger.debug(
                            "Phase2: http_route insert error %s.%s: %s",
                            ctrl.class_name, method.name, exc,
                        )


def _process_method_decorators(
    conn: sqlite3.Connection,
    method: MethodInfo,
    model_name: str,
    module_name: str,
    file_path: str,
) -> None:
    """Insert decorator details for one method."""
    decorator_types: list[str] = []
    api_returns_model: Optional[str] = None
    ormcache_keys: Optional[str] = None

    for dec in method.decorators:
        decorator_types.append(dec.name)
        detail = _parse_decorator_detail(
            dec, model_name, method.name, file_path, method.line_number
        )
        if detail:
            try:
                _insert_decorator_detail(conn, detail)
            except Exception as exc:
                logger.debug(
                    "Phase2: decorators_detail error %s.%s [%s]: %s",
                    model_name, method.name, dec.name, exc,
                )

            # Accumulate method-level data
            if detail.get("returns_model"):
                api_returns_model = detail["returns_model"]
            if detail.get("ormcache_keys"):
                ormcache_keys = detail["ormcache_keys"]

            # Insert context dependencies
            if dec.name == "api.depends_context" and detail.get("depends_ctx_keys"):
                ctx_keys = json.loads(detail["depends_ctx_keys"])
                if ctx_keys:
                    try:
                        _insert_context_dep(
                            conn, model_name, method.name, ctx_keys, module_name, file_path
                        )
                    except Exception as exc:
                        logger.debug("Phase2: context_dep error: %s", exc)

            # Insert http_route for model methods that are also routes
            # (unusual but possible in some patterns)
            if dec.name in ("http.route", "route") and detail.get("http_route"):
                try:
                    _insert_http_route(
                        conn, dec, method, model_name, module_name, file_path
                    )
                except Exception:
                    pass

    # Update methods table
    try:
        _update_method(
            conn,
            model_name,
            method.name,
            module_name,
            decorator_types,
            api_returns_model,
            ormcache_keys,
        )
    except Exception as exc:
        logger.debug(
            "Phase2: update_method error %s.%s: %s", model_name, method.name, exc
        )
