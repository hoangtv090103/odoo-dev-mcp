"""Tool 16: get_project_context — compact AI entry point.

Always call this FIRST in a session.  Returns a ≤400-token JSON map of the
knowledge graph so the AI knows what is available and which tools to use next,
without reading any source files.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional


async def get_project_context(
    focus_model: Optional[str] = None,
    get_db: Callable[[], Path] = None,
    get_config: Callable = None,
) -> dict:
    """Return a compact overview of the Odoo knowledge graph index.

    Args:
        focus_model: Optional model to zoom in on (e.g. 'sale.order').
                     When supplied, the response adds a ``focus`` block with
                     quick facts and a suggested tool-call chain.
        get_db:      Callable returning the SQLite db Path.
        get_config:  Callable returning the ProjectConfig.

    Returns a dict with:
        index       — health, counts, staleness
        overview    — top 15 models, top 10 modules
        navigation  — tool cheat-sheet for the AI
        focus       — (only when focus_model given) quick facts + next steps
    """
    db_path = get_db()

    # ── Index missing ─────────────────────────────────────────────────────────
    if not db_path.exists():
        return {
            "index": {
                "healthy": False,
                "message": "No index found. Build it first.",
            },
            "navigation": {
                "first_step": "build_index()  — builds the knowledge graph",
                "then": "get_project_context()  — call this again after indexing",
            },
        }

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # ── Metadata ──────────────────────────────────────────────────────────
        def _meta(key: str) -> Optional[str]:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

        indexed_at = _meta("indexed_at") or "unknown"
        schema_ver = _meta("schema_version") or "?"
        odoo_ver   = _meta("odoo_version_hint") or "unknown"

        # Detect incomplete index: DB exists but indexing never finished
        # (happens when a prior server run was killed mid-indexing).
        if indexed_at == "unknown":
            return {
                "index": {
                    "healthy": False,
                    "message": (
                        "Index exists but is incomplete — a previous indexing run was "
                        "likely interrupted. The server will rebuild it automatically. "
                        "Wait a few minutes then call get_project_context() again."
                    ),
                    "hint": "You can also force a rebuild with: build_index(reset=True)",
                },
                "navigation": {
                    "first_step": "Wait for auto-rebuild, or call build_index(reset=True)",
                    "then": "get_project_context()  — call this again after indexing completes",
                },
            }

        # ── Counts ────────────────────────────────────────────────────────────
        def _n(table: str) -> int:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        module_count  = _n("modules")
        model_count   = conn.execute(
            "SELECT COUNT(DISTINCT name) FROM models"
        ).fetchone()[0]
        field_count   = _n("fields")
        method_count  = _n("methods")
        view_count    = _n("views")
        route_count   = _n("http_routes")
        sm_count      = _n("state_machines")

        # Stale modules: compare stored hashes vs disk
        stale_count = _stale_count(conn, get_config)

        # ── Top models by complexity (fields + methods) ───────────────────────
        top_rows = conn.execute(
            """
            SELECT
                m.name,
                m.module_name,
                COUNT(DISTINCT f.id)  AS fc,
                COUNT(DISTINCT mt.id) AS mc,
                (SELECT 1 FROM state_machines sm WHERE sm.model_name = m.name LIMIT 1) AS has_sm,
                (SELECT 1 FROM access_rules   ar WHERE ar.model_name = m.name LIMIT 1) AS has_acl
            FROM models m
            LEFT JOIN fields  f  ON f.model_name  = m.name
            LEFT JOIN methods mt ON mt.model_name = m.name
            WHERE m.inherit_type = 'primary'
            GROUP BY m.name
            ORDER BY fc + mc DESC
            LIMIT 15
            """
        ).fetchall()

        top_models = [
            {
                "model":   r["name"],
                "module":  r["module_name"] or "",
                "fields":  r["fc"],
                "methods": r["mc"],
                "sm":      bool(r["has_sm"]),   # has state machine
                "acl":     bool(r["has_acl"]),  # has access rules
            }
            for r in top_rows
        ]

        # ── Top modules by model count ────────────────────────────────────────
        mod_rows = conn.execute(
            """
            SELECT module_name, COUNT(*) AS mc
            FROM models
            WHERE module_name IS NOT NULL AND inherit_type = 'primary'
            GROUP BY module_name
            ORDER BY mc DESC
            LIMIT 10
            """
        ).fetchall()
        top_modules = {r["module_name"]: r["mc"] for r in mod_rows}

        # ── Build response ────────────────────────────────────────────────────
        warnings: list[str] = []
        if stale_count:
            warnings.append(
                f"{stale_count} module(s) changed on disk. "
                "Call build_index() to refresh."
            )
        # Warn when cross-layer data (views/routes) is missing on a non-trivial index
        if model_count > 10 and view_count == 0:
            warnings.append(
                "views=0 on a large codebase is unusual. "
                "If your modules have .xml files, try build_index(reset=True) to reindex."
            )
        if model_count > 10 and route_count == 0:
            warnings.append(
                "routes=0 — no HTTP controllers found. "
                "This is normal if the project has no controllers; otherwise try build_index(reset=True)."
            )

        result: dict = {
            "index": {
                "healthy":      True,
                "last_indexed": indexed_at,
                "odoo_version": odoo_ver,
                "schema":       schema_ver,
                "stale_modules": stale_count,
                "counts": {
                    "modules": module_count,
                    "models":  model_count,
                    "fields":  field_count,
                    "methods": method_count,
                    "views":   view_count,
                    "routes":  route_count,
                    "state_machines": sm_count,
                },
            },
            "overview": {
                "top_models":  top_models,
                "top_modules": top_modules,
            },
            "navigation": _navigation_guide(),
        }

        if warnings:
            result["index"]["warnings"] = warnings

        # ── Focus block ───────────────────────────────────────────────────────
        if focus_model:
            result["focus"] = _focus_block(conn, focus_model)

        return result

    finally:
        conn.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _stale_count(conn, get_config) -> int:
    """Quick stale-module count without re-hashing (uses stored hashes only)."""
    try:
        from .get_index_status import _compute_stale
        cfg = get_config()
        return _compute_stale(conn, cfg)
    except Exception:
        return 0


def _focus_block(conn, model_name: str) -> dict:
    """Compact facts + recommended next tools for a specific model."""
    row = conn.execute(
        "SELECT name, description, module_name, abstract, transient "
        "FROM models WHERE name = ? LIMIT 1",
        (model_name,),
    ).fetchone()

    if not row:
        # Try FTS search as fallback suggestion
        like = conn.execute(
            "SELECT DISTINCT name FROM models WHERE name LIKE ? LIMIT 5",
            (f"%{model_name}%",),
        ).fetchall()
        return {
            "error": f"Model '{model_name}' not found.",
            "suggestions": [r["name"] for r in like],
        }

    fc  = conn.execute("SELECT COUNT(*) FROM fields  WHERE model_name=?", (model_name,)).fetchone()[0]
    mc  = conn.execute("SELECT COUNT(*) FROM methods WHERE model_name=?", (model_name,)).fetchone()[0]
    vc  = conn.execute("SELECT COUNT(*) FROM views   WHERE model=?",      (model_name,)).fetchone()[0]
    has_sm  = bool(conn.execute("SELECT 1 FROM state_machines WHERE model_name=? LIMIT 1", (model_name,)).fetchone())
    has_acl = bool(conn.execute("SELECT 1 FROM access_rules   WHERE model_name=? LIMIT 1", (model_name,)).fetchone())
    has_cron= bool(conn.execute("SELECT 1 FROM cron_jobs      WHERE model_name=? LIMIT 1", (model_name,)).fetchone())

    # Primary module that defines this model
    primary_module = row["module_name"] or ""

    # Inherited models (upstream parents) — parse JSON array if needed
    raw_parents = [r[0] for r in conn.execute(
        "SELECT inherit_model FROM models WHERE name=? AND inherit_model IS NOT NULL",
        (model_name,),
    ).fetchall()]
    parents: list[str] = []
    for p in raw_parents:
        if p and p.startswith("["):
            try:
                items = json.loads(p)
                parents.extend(x for x in items if isinstance(x, str) and x)
            except Exception:
                parents.append(p)
        elif p:
            parents.append(p)

    # Modules that EXTEND this model via _inherit (NOT primary definitions).
    # Each module may add new fields/methods on top of the base model.
    # Group by module_name → list of methods they add.
    ext_rows = conn.execute(
        """
        SELECT m.module_name, mt.method_name
        FROM models m
        LEFT JOIN methods mt ON mt.model_name = m.name AND mt.module_name = m.module_name
        WHERE m.name = ? AND m.inherit_type = '_inherit' AND m.module_name IS NOT NULL
        ORDER BY m.module_name, mt.method_name
        """,
        (model_name,),
    ).fetchall()

    # Build: { module_name: [method1, method2, ...] }
    ext_modules: dict[str, list[str]] = {}
    for r in ext_rows:
        mod = r["module_name"]
        meth = r["method_name"]
        if mod not in ext_modules:
            ext_modules[mod] = []
        if meth and meth not in ext_modules[mod]:
            ext_modules[mod].append(meth)

    # Trim method lists to top 6 per module (keep token count low)
    extending_modules = {
        mod: methods[:6] + (["…"] if len(methods) > 6 else [])
        for mod, methods in sorted(ext_modules.items())
    }

    # Relational connections
    related = conn.execute(
        "SELECT DISTINCT comodel_name FROM fields "
        "WHERE model_name=? AND field_type IN ('Many2one','One2many','Many2many') "
        "AND comodel_name IS NOT NULL LIMIT 8",
        (model_name,),
    ).fetchall()
    related_models = [r[0] for r in related]

    facts = (
        f"{fc} fields, {mc} methods, {vc} views"
        + (", has state machine" if has_sm else "")
        + (", has ACL" if has_acl else "")
        + (", has cron" if has_cron else "")
    )

    # Build suggested tool chain
    suggested: list[str] = [
        f'get_model_schema("{model_name}")           — all fields + methods from every module',
    ]
    if has_sm:
        suggested.append(f'get_state_machine("{model_name}")        — states & transitions')
    if has_acl:
        suggested.append(f'get_access_control("{model_name}")       — ACL & record rules')
    suggested.append(f'get_constraints("{model_name}")            — validation rules')
    if vc:
        suggested.append(f'resolve_xml_view("{model_name}", "form") — merged form view')
    # Always suggest trace_business_flow — it reveals customisations on related models
    # (e.g. np_stock extending stock.picking when analysing sale.order)
    suggested.append(
        f'trace_business_flow("{model_name}")       — downstream O2m/M2m models + who extends each'
    )
    suggested.append(f'analyze_change_impact("{model_name}")      — dependency blast radius')
    if extending_modules:
        for mod in list(extending_modules)[:3]:
            suggested.append(
                f'search_odoo_entities("{mod}", types=["method"]) — methods added by {mod}'
            )

    # Compose note about extensions so AI immediately understands scope
    ext_note: str = ""
    if extending_modules:
        mod_list = ", ".join(extending_modules.keys())
        ext_note = (
            f"{len(extending_modules)} module(s) extend {model_name}: {mod_list}. "
            f"Call get_model_schema(\"{model_name}\") to see ALL fields/methods across every module."
        )
    else:
        ext_note = f"No other modules extend {model_name} (only defined in {primary_module})."

    return {
        "model":              model_name,
        "description":        row["description"] or "",
        "module":             primary_module,
        "abstract":           bool(row["abstract"]),
        "transient":          bool(row["transient"]),
        "quick_facts":        facts,
        "inherits_from":      parents,
        "extending_modules":  extending_modules,   # { module_name: [method1, method2, ...] }
        "extension_note":     ext_note,            # Plain-text hint for AI
        "related_models":     related_models,
        "suggested_tools":    suggested,
    }


def _navigation_guide() -> dict:
    """Static cheat-sheet for the AI — one entry per common task."""
    return {
        "START_HERE":        "get_project_context(focus_model='...')  — always call first",
        "explore_model":     "get_model_schema(model)                 — fields, methods, inheritance",
        "compact_schema":    "get_model_schema(model, compact=True)   — one-line-per-field summary",
        "business_flow":     "trace_business_flow(model)              — downstream O2m/M2m models + who extends each",
        "search":            "search_odoo_entities(query, types=[])   — find anything by name/keyword",
        "follow_graph":      "trace_odoo_path(model, depth=2)         — multi-hop relationship walk",
        "state_machine":     "get_state_machine(model)                — states & transitions",
        "compute_chain":     "trace_compute_chain(model, field)        — why a field recomputes",
        "change_impact":     "analyze_change_impact(model, field)      — blast radius",
        "security":          "get_access_control(model)               — ACL & record rules",
        "http_routes":       "get_http_routes(module=...)              — REST/JSON-RPC endpoints",
        "build_index":       "build_index()                           — (re)build if stale",
        "index_health":      "get_index_status()                      — freshness & counts",
    }
