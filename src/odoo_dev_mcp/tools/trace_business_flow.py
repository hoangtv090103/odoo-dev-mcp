"""Tool 19: trace_business_flow — follow a model's downstream related models and surface customizations.

Complements ``get_project_context(focus_model=...)`` by traversing One2many/Many2many
connections one hop out and revealing how custom modules extend each related model.

Example — ``trace_business_flow("sale.order")`` immediately reveals:
  • sale.order extended by np_sale, np_discount, np_base  (with method lists)
  • stock.picking (via picking_ids)  extended by np_stock  (wave_state, permit flow)
  • account.move  (via invoice_ids) extended by ess_account, sale_invoice_policy
  • stock.move    (via order_line.move_ids) extended by …

Without this tool, an AI would need N separate get_model_schema calls and manual
cross-referencing to discover the same picture.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable


# ── helpers ────────────────────────────────────────────────────────────────────

def _parse_inherit(raw: str | None) -> list[str]:
    """Normalise inherit_model column — may be a plain string or a JSON array."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            items = json.loads(raw)
            return [x for x in items if isinstance(x, str) and x]
        except Exception:
            pass
    return [raw] if raw else []


def _extending_modules(conn, model_name: str, method_limit: int = 6) -> dict[str, dict]:
    """
    Return {module_name: {"methods": [...], "custom_fields": [...]}}
    for every module that does ``_inherit = model_name`` without its own ``_name``.
    """
    rows = conn.execute(
        """
        SELECT m.module_name, mt.method_name
        FROM   models  m
        LEFT JOIN methods mt
               ON  mt.model_name  = m.name
               AND mt.module_name = m.module_name
        WHERE  m.name         = ?
          AND  m.inherit_type = '_inherit'
          AND  m.module_name  IS NOT NULL
        ORDER  BY m.module_name, mt.method_name
        """,
        (model_name,),
    ).fetchall()

    ext: dict[str, list[str]] = {}
    for r in rows:
        mod  = r["module_name"]
        meth = r["method_name"]
        if mod not in ext:
            ext[mod] = []
        if meth and meth not in ext[mod]:
            ext[mod].append(meth)

    result: dict[str, dict] = {}
    for mod, methods in sorted(ext.items()):
        trimmed = methods[:method_limit] + (["…"] if len(methods) > method_limit else [])
        # Custom fields this module adds to the model
        fld_rows = conn.execute(
            """
            SELECT field_name FROM fields
            WHERE  model_name  = ?
              AND  module_name = ?
              AND  field_name  NOT LIKE 'x_%'
            LIMIT 10
            """,
            (model_name, mod),
        ).fetchall()
        result[mod] = {
            "methods":       trimmed,
            "custom_fields": [r["field_name"] for r in fld_rows],
        }
    return result


def _state_summary(conn, model_name: str) -> dict | None:
    """Compact state-machine summary, or None if no state machine."""
    sm = conn.execute(
        "SELECT field_name, states FROM state_machines WHERE model_name = ? LIMIT 1",
        (model_name,),
    ).fetchone()
    if not sm:
        return None
    try:
        states = json.loads(sm["states"] or "[]")
    except Exception:
        states = []
    return {
        "field":       sm["field_name"],
        "state_count": len(states),
        "states":      states[:10],          # cap to avoid bloat
    }


# ── main tool ──────────────────────────────────────────────────────────────────

async def trace_business_flow(
    start_model: str,
    get_db: Callable[[], Path],
) -> dict:
    """Trace downstream business flow: root model + O2m/M2m related models, with customizations.

    Follows One2many and Many2many fields one hop from ``start_model``.  For each
    connected model it returns the list of extension modules (with their method
    names and custom fields), plus the state-machine summary if one exists.

    This is the fastest way to answer "what custom modules touch this business
    process?" without reading individual source files.

    Args:
        start_model: Starting Odoo model (e.g. ``'sale.order'``).
        get_db:      Callable returning the SQLite db Path.

    Returns a dict with::

        root  — root model: module, fields, methods, state_machine, extending_modules
        flow  — list of downstream models (customised first) with the same structure
                plus via_field / relation / is_customized
        next_steps — suggested follow-up tool calls
    """
    db_path = get_db()
    if not db_path.exists():
        return {"error": "Index not found.  Run build_index() first.", "model": start_model}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # ── Verify root model ──────────────────────────────────────────────────
        root_row = conn.execute(
            "SELECT name, module_name, description FROM models WHERE name = ? LIMIT 1",
            (start_model,),
        ).fetchone()
        if not root_row:
            like = conn.execute(
                "SELECT DISTINCT name FROM models WHERE name LIKE ? LIMIT 5",
                (f"%{start_model}%",),
            ).fetchall()
            return {
                "error":       f"Model '{start_model}' not found in index.",
                "suggestions": [r["name"] for r in like],
                "hint":        f'search_odoo_entities("{start_model}")  — find the correct model name',
            }

        root_name = root_row["name"]

        # ── Root quick-facts ───────────────────────────────────────────────────
        root_fc = conn.execute(
            "SELECT COUNT(*) FROM fields  WHERE model_name=?", (root_name,)
        ).fetchone()[0]
        root_mc = conn.execute(
            "SELECT COUNT(*) FROM methods WHERE model_name=?", (root_name,)
        ).fetchone()[0]
        root_ext  = _extending_modules(conn, root_name)
        root_sm   = _state_summary(conn, root_name)

        root_info: dict = {
            "model":             root_name,
            "module":            root_row["module_name"] or "",
            "description":       root_row["description"]  or "",
            "fields":            root_fc,
            "methods":           root_mc,
            "state_machine":     root_sm,
            "extending_modules": root_ext,
        }

        # ── One2many / Many2many connected models (1 hop) ─────────────────────
        rel_rows = conn.execute(
            """
            SELECT field_name, field_type, comodel_name, string_label
            FROM   fields
            WHERE  model_name  = ?
              AND  field_type   IN ('One2many','Many2many')
              AND  comodel_name IS NOT NULL
            ORDER  BY field_type, field_name
            LIMIT  25
            """,
            (root_name,),
        ).fetchall()

        flow: list[dict] = []
        seen: set[str] = set()

        for r in rel_rows:
            comodel = r["comodel_name"]
            if comodel in seen:
                continue
            seen.add(comodel)

            cm_row = conn.execute(
                "SELECT module_name, description FROM models WHERE name = ? LIMIT 1",
                (comodel,),
            ).fetchone()
            if not cm_row:
                continue  # model not indexed (e.g. from an unindexed addon)

            cm_fc  = conn.execute(
                "SELECT COUNT(*) FROM fields  WHERE model_name=?", (comodel,)
            ).fetchone()[0]
            cm_mc  = conn.execute(
                "SELECT COUNT(*) FROM methods WHERE model_name=?", (comodel,)
            ).fetchone()[0]
            cm_ext = _extending_modules(conn, comodel)
            cm_sm  = _state_summary(conn, comodel)

            flow.append({
                "model":             comodel,
                "via_field":         r["field_name"],
                "field_label":       r["string_label"] or "",
                "relation":          r["field_type"],
                "module":            cm_row["module_name"] or "",
                "description":       cm_row["description"]  or "",
                "fields":            cm_fc,
                "methods":           cm_mc,
                "state_machine":     cm_sm,
                "extending_modules": cm_ext,
                "is_customized":     bool(cm_ext),
            })

        # Customised models first, then alphabetical
        flow.sort(key=lambda x: (0 if x["is_customized"] else 1, x["model"]))

        # ── Suggested next steps ───────────────────────────────────────────────
        next_steps: list[str] = []

        # State machines on customised models
        custom_sm = [
            x for x in [root_info] + flow
            if x.get("state_machine") and x.get("extending_modules")
        ]
        for item in custom_sm[:3]:
            m = item["model"]
            sc = item["state_machine"]["state_count"]
            next_steps.append(
                f'get_state_machine("{m}")  — {sc} states, extended by '
                + ", ".join(item["extending_modules"].keys())
            )

        # Most-customised downstream model
        custom_flow = [x for x in flow if x["is_customized"]]
        if custom_flow:
            top = max(custom_flow, key=lambda x: len(x["extending_modules"]))
            n   = len(top["extending_modules"])
            next_steps.append(
                f'get_model_schema("{top["model"]}", compact=True)'
                f'  — {n} module(s) extend it: '
                + ", ".join(top["extending_modules"].keys())
            )

        return {
            "root":       root_info,
            "flow":       flow,
            **({"next_steps": next_steps} if next_steps else {}),
        }

    finally:
        conn.close()
