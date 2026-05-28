"""
Phase 6: JavaScript minimal analysis.

Parses .js files using parse_js_file() and inserts into js_components.
Only processes files in module.js_files (already filtered to
static/src/**/*.js, excluding lib/ and tests/).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..parsers.js_parser import JsComponent, parse_js_file
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── Insertion helper ──────────────────────────────────────────────────────────

def _insert_js_component(conn: sqlite3.Connection, comp: JsComponent) -> None:
    conn.execute(
        """
        INSERT INTO js_components
            (component_type, widget_name, handled_types, component_class,
             action_tag, target_model, target_method, target_route,
             module_name, file_path, line_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            comp.component_type,
            comp.widget_name,
            json.dumps(comp.handled_types),
            comp.component_class,
            comp.action_tag,
            comp.target_model,
            comp.target_method,
            comp.target_route,
            comp.module_name,
            comp.file_path,
            comp.line_number,
        ),
    )


# ── Entry point ────────────────────────────────────────────────────

def run_js(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 6: JavaScript minimal analysis.

    Parses .js files using parse_js_file() and inserts js_components rows.
    Only files in module.js_files are processed (static/src/**/*.js,
    excluding lib/ and tests/).
    """
    total_files = sum(len(m.js_files) for m in modules)
    logger.info("Phase 6: JS analysis for %d modules (%d files)", len(modules), total_files)

    for module in modules:
        if not module.js_files:
            continue
        for js_file in module.js_files:
            try:
                components = parse_js_file(js_file, module.name)
            except Exception as exc:
                logger.warning("Phase6: error parsing %s: %s", js_file, exc)
                continue

            for comp in components:
                try:
                    _insert_js_component(conn, comp)
                except Exception as exc:
                    logger.debug(
                        "Phase6: insert error for %s in %s: %s",
                        comp.component_type, js_file, exc,
                    )

    conn.commit()
    logger.info("Phase 6: complete")
