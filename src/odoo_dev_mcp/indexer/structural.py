"""
Phase 1: Structural indexing.

Parses all .py files for OdooModelInfo and inserts:
  - modules table
  - models table
  - fields table
  - methods table (basic stubs)
  - module_deps table

Uses ThreadPoolExecutor(max_workers=4) for parallel file parsing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from ..parsers.python_parser import (
    OdooModelInfo,
    FieldInfo,
    MethodInfo,
    parse_python_file,
)
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)

_WORKERS = 4


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bool_kwarg(kwargs: dict[str, str], key: str, default: int = 0) -> int:
    """Resolve a boolean-ish kwargs value to 0/1."""
    val = kwargs.get(key, "").strip()
    if not val:
        return default
    if val.lower() in ("true", "1"):
        return 1
    if val.lower() in ("false", "0"):
        return 0
    # Could be a callable or expression — treat as truthy if non-empty
    return 1 if val else default


def _str_kwarg(kwargs: dict[str, str], key: str) -> Optional[str]:
    """Return an unquoted string kwarg value, or None."""
    from ..parsers.python_parser import unquote
    val = kwargs.get(key)
    if val is None:
        return None
    unq = unquote(val)
    return unq if unq is not None else val


def _extract_comodel(field_type: str, kwargs: dict[str, str]) -> Optional[str]:
    """Extract the comodel name for relational fields."""
    if field_type in ("Many2one", "One2many", "Many2many", "Many2oneReference"):
        comodel = kwargs.get("comodel_name")
        if comodel:
            from ..parsers.python_parser import unquote
            return unquote(comodel) or comodel
        # First positional arg may be comodel for Many2one/Many2many
        # (handled as positional_args key in some parsers — skip for now)
    return None


def _extract_selection_values(kwargs: dict[str, str]) -> str:
    """Try to parse selection=[('key','Label'),...] into JSON."""
    selection = kwargs.get("selection", "")
    if not selection or not selection.startswith("["):
        return "[]"
    try:
        import ast
        val = ast.literal_eval(selection)
        if isinstance(val, list):
            return json.dumps(val)
    except Exception:
        pass
    return "[]"


def _extract_selection_add(kwargs: dict[str, str]) -> str:
    """Parse selection_add=[...] into JSON."""
    sel_add = kwargs.get("selection_add", "")
    if not sel_add or not sel_add.startswith("["):
        return "[]"
    try:
        import ast
        val = ast.literal_eval(sel_add)
        if isinstance(val, list):
            return json.dumps(val)
    except Exception:
        pass
    return "[]"


def _extract_digits(kwargs: dict[str, str]) -> Optional[str]:
    """Parse digits=(16,2) or digits=2 into JSON."""
    digits = kwargs.get("digits")
    if not digits:
        return None
    try:
        import ast
        val = ast.literal_eval(digits)
        if isinstance(val, (list, tuple)):
            return json.dumps(list(val))
        if isinstance(val, int):
            return json.dumps([16, val])
    except Exception:
        pass
    return digits


# ── Module insertion ──────────────────────────────────────────────────────────

def _upsert_module(conn: sqlite3.Connection, module: ModuleRecord) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO modules
            (name, path, version, category, depends, auto_install,
             installable, application, summary, description, author, website)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            module.name,
            str(module.path),
            module.version,
            module.category,
            json.dumps(module.depends),
            1 if module.auto_install else 0,
            1 if module.installable else 0,
            1 if module.application else 0,
            module.summary,
            module.description,
            module.author,
            module.website,
        ),
    )


# ── Model insertion ───────────────────────────────────────────────────────────

def _upsert_model(
    conn: sqlite3.Connection,
    model: OdooModelInfo,
    module_name: str,
    inherit_model: Optional[str],
    inherit_type: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO models
            (name, python_class, inherit_type, inherit_model, inherits_map,
             description, table_name, rec_name, order_field,
             abstract, transient, module_name, file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model.model_name or inherit_model,
            model.class_name,
            inherit_type,
            inherit_model,
            json.dumps(model.inherits or {}),
            model.description,
            model.table_name,
            model.rec_name,
            model.order,
            1 if model.is_abstract else 0,
            1 if model.is_transient else 0,
            module_name,
            model.file_path,
            model.line_number,
        ),
    )


# ── Field insertion ───────────────────────────────────────────────────────────

def _upsert_field(
    conn: sqlite3.Connection,
    f: FieldInfo,
    model_name: str,
    module_name: str,
    file_path: str,
) -> None:
    kw = f.kwargs

    # Boolean attributes
    required = _bool_kwarg(kw, "required")
    readonly = _bool_kwarg(kw, "readonly")
    store_default = 0 if kw.get("compute") else 1
    store = _bool_kwarg(kw, "store", store_default)
    index_field = _bool_kwarg(kw, "index")
    copy_field = _bool_kwarg(kw, "copy", 1)
    tracking_raw = kw.get("tracking", kw.get("track_visibility"))
    tracking = 1 if tracking_raw and tracking_raw.lower() not in ("false", "0", "none") else 0
    delegate = _bool_kwarg(kw, "delegate")

    # String attributes
    comodel_name = _extract_comodel(f.field_type, kw)
    string_label = _str_kwarg(kw, "string")
    compute = _str_kwarg(kw, "compute")
    inverse = _str_kwarg(kw, "inverse")
    search = _str_kwarg(kw, "search")
    related = _str_kwarg(kw, "related")
    help_text = _str_kwarg(kw, "help")
    groups = _str_kwarg(kw, "groups")
    ondelete_behavior = _str_kwarg(kw, "ondelete")
    domain_expr = _str_kwarg(kw, "domain")
    currency_field = _str_kwarg(kw, "currency_field")
    default_val = kw.get("default")

    # JSON attributes
    selection_values = _extract_selection_values(kw)
    selection_add = _extract_selection_add(kw)
    digits = _extract_digits(kw)

    # States visibility
    states_raw = kw.get("states")
    states_visibility = states_raw if states_raw else "{}"

    conn.execute(
        """
        INSERT OR REPLACE INTO fields
            (model_name, field_name, field_type, comodel_name, string_label,
             required, readonly, store, index_field, compute, inverse, search,
             related, help_text, copy_field, tracking, groups, states_visibility,
             selection_values, selection_add, ondelete_behavior, domain_expr,
             delegate, currency_field, digits, default_val,
             module_name, file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_name,
            f.name,
            f.field_type,
            comodel_name,
            string_label,
            required,
            readonly,
            store,
            index_field,
            compute,
            inverse,
            search,
            related,
            help_text,
            copy_field,
            tracking,
            groups,
            states_visibility,
            selection_values,
            selection_add,
            ondelete_behavior,
            domain_expr,
            delegate,
            currency_field,
            digits,
            default_val,
            module_name,
            file_path,
            f.line_number,
        ),
    )


# ── Method stub insertion ─────────────────────────────────────────────────────

def _upsert_method_stub(
    conn: sqlite3.Connection,
    method: MethodInfo,
    model_name: str,
    module_name: str,
    file_path: str,
) -> None:
    decorator_types = json.dumps([d.name for d in method.decorators])
    conn.execute(
        """
        INSERT OR IGNORE INTO methods
            (model_name, method_name, decorator_types,
             module_name, file_path, line_number,
             body_start_line, body_end_line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model_name,
            method.name,
            decorator_types,
            module_name,
            file_path,
            method.line_number,
            method.body_start,
            method.body_end,
        ),
    )


# ── Module deps insertion ─────────────────────────────────────────────────────

def _insert_module_deps(conn: sqlite3.Connection, module: ModuleRecord) -> None:
    for dep in module.depends:
        conn.execute(
            "INSERT OR IGNORE INTO module_deps (module_name, depends_on) VALUES (?, ?)",
            (module.name, dep),
        )


# ── Per-file parse task ───────────────────────────────────────────────────────

def _parse_file_task(
    py_file: Path,
    module_name: str,
) -> tuple[Path, list[OdooModelInfo], list, list[str]]:
    """Worker task: parse one Python file. Returns (path, models, controllers, errors)."""
    errors: list[str] = []
    try:
        models, controllers = parse_python_file(py_file)
        return py_file, models, controllers, errors
    except Exception as exc:
        errors.append(f"{py_file}: {exc}")
        return py_file, [], [], errors


# ── Entry point ────────────────────────────────────────────────────

def run_structural(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 1: Structural indexing.

    - Insert/upsert modules table
    - Parse all .py files for OdooModelInfo (parallel, 4 workers)
    - Insert models, fields, basic method stubs
    - Insert module_deps
    """
    logger.info("Phase 1: structural indexing of %d modules", len(modules))

    errors: list[str] = []

    for module in modules:
        try:
            _upsert_module(conn, module)
            _insert_module_deps(conn, module)
        except Exception as exc:
            logger.warning("Phase1: error upserting module %s: %s", module.name, exc)
            errors.append(f"module {module.name}: {exc}")

    conn.commit()

    # Collect all (file, module_name) pairs
    file_tasks: list[tuple[Path, str]] = [
        (py_file, module.name)
        for module in modules
        for py_file in module.python_files
    ]

    logger.info("Phase 1: parsing %d Python files with %d workers", len(file_tasks), _WORKERS)

    # Build a lookup: file_path -> module_name
    file_module_map: dict[str, str] = {
        str(path): mod_name for path, mod_name in file_tasks
    }

    with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
        futures = {
            executor.submit(_parse_file_task, path, mod_name): (path, mod_name)
            for path, mod_name in file_tasks
        }

        batch: list[tuple[OdooModelInfo, str]] = []
        batch_size = 50

        for future in as_completed(futures):
            py_file, mod_name = futures[future]
            try:
                _, models, controllers, parse_errors = future.result()
            except Exception as exc:
                logger.warning("Phase1: future error for %s: %s", py_file, exc)
                errors.append(f"{py_file}: {exc}")
                continue

            errors.extend(parse_errors)

            for model in models:
                batch.append((model, mod_name))

            if len(batch) >= batch_size:
                _flush_model_batch(conn, batch)
                batch.clear()

        if batch:
            _flush_model_batch(conn, batch)

    conn.commit()
    if errors:
        logger.warning("Phase 1 completed with %d errors", len(errors))


def _flush_model_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[OdooModelInfo, str]],
) -> None:
    """Insert a batch of (OdooModelInfo, module_name) into DB."""
    for model, module_name in batch:
        try:
            _process_model(conn, model, module_name)
        except Exception as exc:
            logger.warning(
                "Phase1: error processing model %s in %s: %s",
                model.model_name or model.class_name,
                module_name,
                exc,
            )


def _process_model(
    conn: sqlite3.Connection,
    model: OdooModelInfo,
    module_name: str,
) -> None:
    """Insert/upsert a single OdooModelInfo and its fields/methods."""
    inherit = model.inherit

    # Determine inherit_type and canonical model name
    if model.model_name and not inherit:
        # Plain definition: _name set, no _inherit
        _upsert_model(conn, model, module_name, None, "primary")
        effective_name = model.model_name

    elif model.model_name and inherit:
        # _name + _inherit: primary definition that also inherits
        _upsert_model(conn, model, module_name, None, "primary")
        effective_name = model.model_name
        # Also record the inheritance relationship(s)
        if isinstance(inherit, str):
            _record_inherit(conn, model, module_name, inherit)
        elif isinstance(inherit, list):
            for inh in inherit:
                _record_inherit(conn, model, module_name, inh)

    elif isinstance(inherit, str):
        # Only _inherit (string) — extending existing model
        _upsert_model(conn, model, module_name, inherit, "_inherit")
        effective_name = inherit

    elif isinstance(inherit, list):
        # _inherit is a list — insert one row per inherited model
        for inh in inherit:
            model_copy = model
            _upsert_model_with_name(conn, model_copy, module_name, inh, "_inherit")
        # Use first as effective name for fields/methods
        effective_name = inherit[0] if inherit else None
        if not effective_name:
            return
    else:
        # No _name, no _inherit — not a real Odoo model, skip
        return

    # Insert fields
    for f in model.fields:
        try:
            _upsert_field(conn, f, effective_name, module_name, model.file_path)
        except Exception as exc:
            logger.debug(
                "Phase1: field %s.%s error: %s", effective_name, f.name, exc
            )

    # Insert method stubs
    for method in model.methods:
        try:
            _upsert_method_stub(
                conn, method, effective_name, module_name, model.file_path
            )
        except Exception as exc:
            logger.debug(
                "Phase1: method %s.%s error: %s", effective_name, method.name, exc
            )


def _record_inherit(
    conn: sqlite3.Connection,
    model: OdooModelInfo,
    module_name: str,
    inherit_model: str,
) -> None:
    """Record an _inherit relationship as a separate row (extension)."""
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO models
                (name, python_class, inherit_type, inherit_model,
                 module_name, file_path, line_number)
            VALUES (?, ?, '_inherit', ?, ?, ?, ?)
            """,
            (
                inherit_model,
                model.class_name,
                inherit_model,
                module_name,
                model.file_path,
                model.line_number,
            ),
        )
    except Exception as exc:
        logger.debug("Phase1: _record_inherit error: %s", exc)


def _upsert_model_with_name(
    conn: sqlite3.Connection,
    model: OdooModelInfo,
    module_name: str,
    inherit_model: str,
    inherit_type: str,
) -> None:
    """Upsert a model row using a specific inherit_model as the model name."""
    conn.execute(
        """
        INSERT OR REPLACE INTO models
            (name, python_class, inherit_type, inherit_model, inherits_map,
             description, table_name, rec_name, order_field,
             abstract, transient, module_name, file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inherit_model,
            model.class_name,
            inherit_type,
            inherit_model,
            json.dumps(model.inherits or {}),
            model.description,
            model.table_name,
            model.rec_name,
            model.order,
            1 if model.is_abstract else 0,
            1 if model.is_transient else 0,
            module_name,
            model.file_path,
            model.line_number,
        ),
    )
