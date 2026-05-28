"""Comprehensive knowledge graph visualizer — v2.

Performance improvements over v1:
  - Batch SQL: all entity data pre-fetched in bulk queries (eliminates N+1 per-model pattern)
  - vis.DataView filters: filter toggles call DataView.refresh() instead of
    mass nodes.update() on ALL nodes — O(visible) not O(total)
  - Auto-tune: physics disabled + hierarchical layout when node count > 150

New entity coverage:
  - Security summary node per model: access_rules + record_rules counts
  - Cron node per model: scheduled jobs
  - Full-graph (--all) mode: all models in codebase with inherit + field relations

New UX:
  - Zoom +/- toolbar buttons
  - PNG canvas export button
  - Richer info panel (structured HTML tables populated from embedded nodeData)
  - Cluster toggle: hide method/view/state nodes to reduce visual noise
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Visual style constants
# ─────────────────────────────────────────────────────────────────────────────

_NODE_STYLE: dict[str, dict] = {
    "model": {
        "shape": "box",
        "color": {"background": "#dbeafe", "border": "#3b82f6",
                  "highlight": {"background": "#bfdbfe", "border": "#2563eb"}},
        "font": {"color": "#1e3a5f", "size": 13, "bold": False},
        "borderWidth": 2,
    },
    "method": {
        "shape": "ellipse",
        "color": {"background": "#ede9fe", "border": "#8b5cf6",
                  "highlight": {"background": "#ddd6fe", "border": "#7c3aed"}},
        "font": {"color": "#2e1065", "size": 11},
        "borderWidth": 1,
    },
    "view": {
        "shape": "hexagon",
        "color": {"background": "#fff7ed", "border": "#f97316",
                  "highlight": {"background": "#fed7aa", "border": "#ea580c"}},
        "font": {"color": "#431407", "size": 11},
        "borderWidth": 1,
    },
    "state": {
        "shape": "dot",
        "color": {"background": "#dcfce7", "border": "#22c55e",
                  "highlight": {"background": "#bbf7d0", "border": "#16a34a"}},
        "font": {"color": "#14532d", "size": 11, "bold": True},
        "borderWidth": 2,
        "size": 18,
    },
    "action": {
        "shape": "box",
        "color": {"background": "#fce7f3", "border": "#ec4899",
                  "highlight": {"background": "#fbcfe8", "border": "#db2777"}},
        "font": {"color": "#500724", "size": 11},
        "borderWidth": 1,
        "borderRadius": 8,
    },
    "module": {
        "shape": "database",
        "color": {"background": "#f0fdf4", "border": "#16a34a",
                  "highlight": {"background": "#dcfce7", "border": "#15803d"}},
        "font": {"color": "#052e16", "size": 12},
        "borderWidth": 1,
    },
    "security": {
        "shape": "triangle",
        "color": {"background": "#fef9c3", "border": "#ca8a04",
                  "highlight": {"background": "#fef08a", "border": "#a16207"}},
        "font": {"color": "#422006", "size": 10},
        "borderWidth": 1,
        "size": 16,
    },
    "cron": {
        "shape": "diamond",
        "color": {"background": "#e0f2fe", "border": "#0284c7",
                  "highlight": {"background": "#bae6fd", "border": "#0369a1"}},
        "font": {"color": "#0c4a6e", "size": 10},
        "borderWidth": 1,
        "size": 16,
    },
}

_EDGE_COLOR: dict[str, str] = {
    "inherit":      "#6366f1",   # indigo  - inheritance
    "field_rel":    "#3b82f6",   # blue    - Many2one/O2m/M2m
    "compute":      "#8b5cf6",   # violet  - field compute
    "constrains":   "#f43f5e",   # rose    - @api.constrains
    "onchange":     "#f97316",   # orange  - @api.onchange
    "action_meth":  "#ec4899",   # pink    - action method
    "view_model":   "#fb923c",   # orange  - view → model
    "view_inherit": "#fed7aa",   # light   - view inherit
    "state_trans":  "#22c55e",   # green   - state transition
    "has_state":    "#86efac",   # light   - model → state
    "action_tgt":   "#f472b6",   # pink    - action targets model
    "module_dep":   "#94a3b8",   # gray    - module dependency
    "defined_in":   "#64748b",   # dark    - model → module
    "security":     "#ca8a04",   # yellow  - model → security
    "cron":         "#0284c7",   # blue    - model → cron
}


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────

class GraphBuilder:
    """Incrementally builds nodes + edges with deduplication."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}    # id → vis node
        self.edges: list[dict] = []
        self._edge_keys: set[tuple] = set()
        self._eid = 0

    def node(
        self,
        node_id: str,
        label: str,
        node_type: str,
        title: str = "",
        is_root: bool = False,
        group: str = "",
        node_data: Optional[dict] = None,
    ) -> None:
        if node_id in self.nodes:
            return
        style = {k: v for k, v in _NODE_STYLE.get(node_type, _NODE_STYLE["model"]).items()}
        style["color"] = dict(style["color"])
        style["color"]["highlight"] = dict(style["color"].get("highlight", {}))
        style["font"] = dict(style.get("font", {}))

        if is_root:
            style["borderWidth"] = 4
            style["font"] = {**style["font"], "bold": True, "size": 15}
            style["color"] = {**style["color"], "background": "#c7d2fe", "border": "#4f46e5"}

        entry: dict = {
            "id": node_id,
            "label": label,
            "group": group or node_type,
            "title": title or label,
            **style,
        }
        if node_data:
            entry["_data"] = node_data  # Rich data for info panel (will be in nodeData JS obj)
        self.nodes[node_id] = entry

    def edge(
        self,
        from_id: str,
        to_id: str,
        label: str = "",
        edge_type: str = "default",
        dashes: bool = False,
        width: float = 1.5,
    ) -> None:
        key = (from_id, to_id, label, edge_type)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self._eid += 1
        color = _EDGE_COLOR.get(edge_type, "#94a3b8")
        self.edges.append({
            "id": self._eid,
            "from": from_id,
            "to": to_id,
            "label": label,
            "title": f"{from_id} → {to_id}" + (f" [{label}]" if label else ""),
            "arrows": "to",
            "color": {"color": color, "opacity": 0.85},
            "dashes": dashes,
            "width": width,
            "font": {"size": 9, "color": "#64748b", "background": "rgba(0,0,0,0)"},
            "smooth": {"enabled": True, "type": "dynamic"},
            "edgeType": edge_type,
        })

    def to_dict(self, title: str = "Odoo Knowledge Graph") -> dict:
        # Separate node_data (rich info) from vis node properties
        nodes_vis = []
        node_data: dict[str, dict] = {}
        for nid, n in self.nodes.items():
            vis_node = {k: v for k, v in n.items() if k != "_data"}
            nodes_vis.append(vis_node)
            if "_data" in n:
                node_data[nid] = n["_data"]
        return {
            "title": title,
            "nodes": nodes_vis,
            "edges": self.edges,
            "node_data": node_data,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _q(s: str | None) -> str:
    return (s or "").replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")


def _rows(conn, sql: str, params=()) -> list:
    return conn.execute(sql, params).fetchall()


def _in_clause(items: list) -> tuple[str, list]:
    """Return ('?,?,?', [a,b,c]) for an IN clause."""
    if not items:
        return "''", []
    return ",".join("?" * len(items)), list(items)


def _jl(raw: str | None) -> list:
    """Safely parse a JSON list column."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# BFS model collection
# ─────────────────────────────────────────────────────────────────────────────

def _parse_inherit(raw: str | None) -> list[str]:
    """Normalise inherit_model (plain string or JSON array) → list of model names."""
    if not raw or not raw.strip():
        return []
    if raw.startswith("["):
        try:
            vals = json.loads(raw)
            return [v for v in vals if isinstance(v, str) and v]
        except Exception:
            return []
    return [raw.strip()]


def _collect_models_bfs(conn, start_model: str, depth: int) -> set[str]:
    """Collect all model names within `depth` hops via inheritance and field relations."""
    visited: set[str] = set()
    queue = [(start_model, depth)]

    while queue:
        model, d = queue.pop(0)
        if model in visited:
            continue
        visited.add(model)
        if d <= 0:
            continue

        # Inheritance parents (inherit_model may be a JSON array)
        for r in _rows(conn,
            "SELECT inherit_model FROM models WHERE name=? AND inherit_model IS NOT NULL AND inherit_model!=''",
            (model,)):
            for parent in _parse_inherit(r["inherit_model"]):
                if parent not in visited:
                    queue.append((parent, d - 1))

        # Inheritance children
        for r in _rows(conn,
            "SELECT name FROM models WHERE inherit_model=? AND inherit_type='_inherit'",
            (model,)):
            child = r["name"]
            if child and child not in visited:
                queue.append((child, d - 1))

        # Relational field targets
        for r in _rows(conn,
            """SELECT DISTINCT comodel_name FROM fields
               WHERE model_name=? AND field_type IN ('Many2one','One2many','Many2many','Many2oneReference')
               AND comodel_name IS NOT NULL AND comodel_name != ''""",
            (model,)):
            target = r["comodel_name"]
            if target and target not in visited:
                queue.append((target, d - 1))

    return visited


# ─────────────────────────────────────────────────────────────────────────────
# Batch SQL fetch — all data in one pass
# ─────────────────────────────────────────────────────────────────────────────

def _batch_fetch(conn, model_names: list[str]) -> dict:
    """Fetch ALL entity data for a list of models with bulk SQL queries."""
    ph, params = _in_clause(model_names)

    def q(sql: str, p=None) -> list:
        return _rows(conn, sql, p if p is not None else params)

    models = q(f"""
        SELECT name, description, module_name, abstract, transient,
               inherit_model, inherit_type
        FROM models WHERE name IN ({ph})
    """)

    field_counts = q(f"""
        SELECT model_name, COUNT(*) AS cnt FROM fields
        WHERE model_name IN ({ph}) GROUP BY model_name
    """)

    method_counts = q(f"""
        SELECT model_name, COUNT(*) AS cnt FROM methods
        WHERE model_name IN ({ph}) GROUP BY model_name
    """)

    inherits = q(f"""
        SELECT name, inherit_model, inherit_type FROM models
        WHERE name IN ({ph}) AND inherit_model IS NOT NULL AND inherit_model!=''
    """)

    inherit_children = q(f"""
        SELECT name, inherit_model FROM models
        WHERE inherit_model IN ({ph}) AND inherit_type='_inherit'
    """, params)  # same params but checking inherit_model IN (model_names)

    rel_fields = q(f"""
        SELECT model_name, field_name, field_type, comodel_name, string_label
        FROM fields
        WHERE model_name IN ({ph})
          AND field_type IN ('Many2one','One2many','Many2many','Many2oneReference')
          AND comodel_name IS NOT NULL AND comodel_name != ''
        ORDER BY model_name, field_type, field_name
    """)

    compute_fields = q(f"""
        SELECT model_name, field_name, field_type, compute
        FROM fields WHERE model_name IN ({ph}) AND compute IS NOT NULL AND compute != ''
    """)

    decorators = q(f"""
        SELECT model_name, method_name, decorator_type,
               depends_fields, constrains_fields, onchange_fields
        FROM decorators_detail
        WHERE model_name IN ({ph})
          AND decorator_type IN ('api.depends','api.constrains','api.onchange')
    """)

    action_methods = q(f"""
        SELECT model_name, method_name, state_transitions
        FROM methods
        WHERE model_name IN ({ph})
          AND state_transitions IS NOT NULL AND state_transitions != '[]'
    """)

    views = q(f"""
        SELECT xml_id, view_type, inherit_id, module_name, model, name
        FROM views WHERE model IN ({ph})
        ORDER BY inherit_id IS NOT NULL, view_type
    """)

    state_machines = q(f"""
        SELECT model_name, field_name, states, transitions
        FROM state_machines WHERE model_name IN ({ph})
    """)

    actions = q(f"""
        SELECT xml_id, action_type, name, module_name, res_model
        FROM actions WHERE res_model IN ({ph}) AND action_type='act_window'
        LIMIT 200
    """)

    access_rules = q(f"""
        SELECT model_name, name, group_xml_id, perm_read, perm_write, perm_create, perm_unlink
        FROM access_rules WHERE model_name IN ({ph})
        ORDER BY model_name, name
    """)

    record_rules = q(f"""
        SELECT model_name, name, domain_force, groups, perm_read, perm_write, perm_create, perm_unlink
        FROM record_rules WHERE model_name IN ({ph})
        ORDER BY model_name, name
    """)

    cron_jobs = q(f"""
        SELECT model_name, xml_id, name, method_name, interval_number, interval_type, active
        FROM cron_jobs WHERE model_name IN ({ph})
        ORDER BY model_name, name
    """)

    # Module-level data (modules referenced by model list)
    module_names = list({dict(m)["module_name"] for m in models if dict(m).get("module_name")})
    modules_info: list = []
    module_deps: list = []
    if module_names:
        mph, mparams = _in_clause(module_names)
        modules_info = _rows(conn, f"SELECT name, category, application FROM modules WHERE name IN ({mph})", mparams)
        module_deps = _rows(conn, f"SELECT module_name, depends_on FROM module_deps WHERE module_name IN ({mph})", mparams)

    return {
        "models": [dict(r) for r in models],
        "field_counts": {r["model_name"]: r["cnt"] for r in field_counts},
        "method_counts": {r["model_name"]: r["cnt"] for r in method_counts},
        "inherits": [dict(r) for r in inherits],
        "inherit_children": [dict(r) for r in inherit_children],
        "rel_fields": [dict(r) for r in rel_fields],
        "compute_fields": [dict(r) for r in compute_fields],
        "decorators": [dict(r) for r in decorators],
        "action_methods": [dict(r) for r in action_methods],
        "views": [dict(r) for r in views],
        "state_machines": [dict(r) for r in state_machines],
        "actions": [dict(r) for r in actions],
        "access_rules": [dict(r) for r in access_rules],
        "record_rules": [dict(r) for r in record_rules],
        "cron_jobs": [dict(r) for r in cron_jobs],
        "modules_info": {r["name"]: dict(r) for r in modules_info},
        "module_deps": [dict(r) for r in module_deps],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Graph assembler — builds vis nodes/edges from pre-fetched batch data
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph(
    g: GraphBuilder,
    data: dict,
    root_model: Optional[str] = None,
    all_models_mode: bool = False,
) -> None:
    """Populate GraphBuilder from pre-fetched batch data."""

    fc = data["field_counts"]
    mc = data["method_counts"]

    # ── Model nodes ───────────────────────────────────────────────────────────
    # Pre-group security and cron by model for info panel
    acl_by_model: dict[str, list] = {}
    for r in data["access_rules"]:
        acl_by_model.setdefault(r["model_name"], []).append(r)
    rrule_by_model: dict[str, list] = {}
    for r in data["record_rules"]:
        rrule_by_model.setdefault(r["model_name"], []).append(r)
    cron_by_model: dict[str, list] = {}
    for r in data["cron_jobs"]:
        cron_by_model.setdefault(r["model_name"], []).append(r)

    model_set = {r["name"] for r in data["models"]}

    for r in data["models"]:
        mn = r["name"]
        mod = r.get("module_name") or ""
        desc = r.get("description") or ""
        abstract = bool(r.get("abstract"))
        transient = bool(r.get("transient"))
        n_fields = fc.get(mn, 0)
        n_methods = mc.get(mn, 0)
        n_acl = len(acl_by_model.get(mn, []))
        n_rrules = len(rrule_by_model.get(mn, []))
        n_cron = len(cron_by_model.get(mn, []))

        tooltip = (
            f"<b>{_q(mn)}</b>"
            + (f"<br><i>{_q(desc)}</i>" if desc else "")
            + f"<br>Module: <b>{_q(mod)}</b>"
            + f"<br>Fields: {n_fields} · Methods: {n_methods}"
            + (f" · ACL: {n_acl}" if n_acl else "")
            + (f" · Cron: {n_cron}" if n_cron else "")
            + (" <i>(abstract)</i>" if abstract else "")
            + (" <i>(transient)</i>" if transient else "")
        )

        # Build rich node_data for info panel
        node_info: dict = {
            "type": "model",
            "model": mn,
            "module": mod,
            "description": desc,
            "field_count": n_fields,
            "method_count": n_methods,
            "abstract": abstract,
            "transient": transient,
        }

        # ACL summary
        if acl_by_model.get(mn):
            acl_rows = acl_by_model[mn]
            node_info["acl"] = [
                {
                    "name": r2.get("name", ""),
                    "group": r2.get("group_xml_id") or "(all users)",
                    "perms": "".join([
                        "R" if r2.get("perm_read") else "-",
                        "W" if r2.get("perm_write") else "-",
                        "C" if r2.get("perm_create") else "-",
                        "D" if r2.get("perm_unlink") else "-",
                    ])
                } for r2 in acl_rows[:10]
            ]

        # Record rules summary
        if rrule_by_model.get(mn):
            node_info["record_rules"] = [
                {
                    "name": r2.get("name", ""),
                    "domain": (r2.get("domain_force") or "")[:80],
                }
                for r2 in rrule_by_model[mn][:5]
            ]

        # Cron summary
        if cron_by_model.get(mn):
            node_info["cron"] = [
                {
                    "name": r2.get("name") or r2.get("xml_id", ""),
                    "method": r2.get("method_name", ""),
                    "interval": f"{r2.get('interval_number', 1)} {r2.get('interval_type', '')}",
                    "active": bool(r2.get("active", 1)),
                }
                for r2 in cron_by_model[mn]
            ]

        g.node(
            f"m:{mn}", mn, "model", tooltip,
            is_root=(mn == root_model),
            group=mod,
            node_data=node_info,
        )

        # Module node
        if mod:
            mod_info = data["modules_info"].get(mod, {})
            cat = mod_info.get("category") or ""
            is_app = bool(mod_info.get("application"))
            g.node(
                f"mod:{mod}", mod, "module",
                f"<b>Module: {_q(mod)}</b>"
                + (f"<br>Category: {_q(cat)}" if cat else "")
                + (" · app" if is_app else ""),
            )
            if not all_models_mode:
                g.edge(f"m:{mn}", f"mod:{mod}", "defined in", "defined_in", dashes=True)

    # ── Inheritance edges ─────────────────────────────────────────────────────
    for r in data["inherits"]:
        child = r["name"]
        itype = r.get("inherit_type") or "_inherit"
        for parent in _parse_inherit(r["inherit_model"]):
            g.edge(f"m:{child}", f"m:{parent}", itype, "inherit", dashes=True)
            # Stub parent node if not in main model set
            if parent not in model_set:
                g.node(f"m:{parent}", parent, "model",
                       f"<b>{_q(parent)}</b><br><i>(external)</i>")

    for r in data["inherit_children"]:
        child = r["name"]
        parent = r["inherit_model"]
        if child in model_set and parent in model_set:
            g.edge(f"m:{child}", f"m:{parent}", "_inherit", "inherit", dashes=True)

    # ── Relational field edges ────────────────────────────────────────────────
    _FT_SHORT = {"Many2one": "M2o", "One2many": "O2m", "Many2many": "M2m"}
    for r in data["rel_fields"]:
        src = r["model_name"]
        target = r["comodel_name"]
        fname = r["field_name"]
        ftype = r.get("field_type", "")
        short = _FT_SHORT.get(ftype, ftype[:3])
        g.edge(f"m:{src}", f"m:{target}", f"{fname} ({short})", "field_rel")
        # Stub target if not in main set
        if target not in model_set:
            g.node(f"m:{target}", target, "model",
                   f"<b>{_q(target)}</b><br><i>(external)</i>")

    # Stop here for all_models_mode — no sub-nodes for performance
    if all_models_mode:
        return

    # ── Method nodes ──────────────────────────────────────────────────────────
    # Compute fields
    dep_fields_by_method: dict[tuple, list] = {}
    for d in data["decorators"]:
        if d["decorator_type"] == "api.depends":
            key = (d["model_name"], d["method_name"])
            dep_fields_by_method[key] = _jl(d.get("depends_fields"))

    for r in data["compute_fields"]:
        mn = r["model_name"]
        meth = r["compute"]
        if not meth:
            continue
        mid = f"mt:{mn}.{meth}"
        deps = dep_fields_by_method.get((mn, meth), [])
        g.node(
            mid, f"⚡ {meth}", "method",
            (
                f"<b>compute: {_q(meth)}</b>"
                f"<br>Model: {_q(mn)}"
                f"<br>For field: {_q(r['field_name'])} ({r.get('field_type','')})"
                + (f"<br>@depends: {', '.join(deps[:5])}" if deps else "")
            ),
            group=mn,
            node_data={"type": "method", "subtype": "compute", "model": mn,
                       "method": meth, "field": r["field_name"], "depends": deps},
        )
        g.edge(f"m:{mn}", mid, f"compute\n{r['field_name']}", "compute", dashes=True)

    # Constrains / onchange
    for d in data["decorators"]:
        mn = d["model_name"]
        meth = d["method_name"]
        dtype = d["decorator_type"]
        mid = f"mt:{mn}.{meth}"
        if dtype == "api.constrains":
            fields = _jl(d.get("constrains_fields"))
            g.node(
                mid, f"🔒 {meth}", "method",
                f"<b>@constrains</b><br>Model: {_q(mn)}<br>Fields: {', '.join(fields[:5])}",
                group=mn,
                node_data={"type": "method", "subtype": "constrains",
                           "model": mn, "method": meth, "fields": fields},
            )
            g.edge(f"m:{mn}", mid, "constrains", "constrains")
        elif dtype == "api.onchange":
            fields = _jl(d.get("onchange_fields"))
            g.node(
                mid, f"🔄 {meth}", "method",
                f"<b>@onchange</b><br>Model: {_q(mn)}<br>Fields: {', '.join(fields[:5])}",
                group=mn,
                node_data={"type": "method", "subtype": "onchange",
                           "model": mn, "method": meth, "fields": fields},
            )
            g.edge(f"m:{mn}", mid, "onchange", "onchange")

    # Action methods (with state transitions)
    for r in data["action_methods"]:
        mn = r["model_name"]
        meth = r["method_name"]
        trans = _jl(r.get("state_transitions"))
        mid = f"mt:{mn}.{meth}"
        froms = {t.get("from", "?") for t in trans}
        tos = {t.get("to", "?") for t in trans}
        summary = " → ".join(f"{f}→{t}" for f, t in zip(list(froms)[:3], list(tos)[:3]))
        g.node(
            mid, f"▶ {meth}", "method",
            (
                f"<b>action: {_q(meth)}</b>"
                f"<br>Model: {_q(mn)}"
                + (f"<br>Transitions: {summary}" if summary else "")
            ),
            group=mn,
            node_data={"type": "method", "subtype": "action",
                       "model": mn, "method": meth, "transitions": trans},
        )
        g.edge(f"m:{mn}", mid, "action", "action_meth")

    # ── View nodes ────────────────────────────────────────────────────────────
    VIEW_ICONS = {"form": "📋", "list": "📄", "kanban": "🗂", "search": "🔍",
                  "tree": "📄", "pivot": "📊", "graph": "📈", "calendar": "📅"}
    for r in data["views"]:
        xml_id = r.get("xml_id") or ""
        if not xml_id:
            continue
        vtype = r.get("view_type") or "unknown"
        vid = f"v:{xml_id}"
        parent_id = r.get("inherit_id") or ""
        mod = r.get("module_name") or ""
        icon = VIEW_ICONS.get(vtype, "👁")
        short = xml_id.split(".")[-1] if "." in xml_id else xml_id
        g.node(
            vid,
            f"{icon} {short}\n({vtype})",
            "view",
            f"<b>{_q(xml_id)}</b><br>Type: {vtype}"
            + f"<br>Model: {_q(r.get('model',''))}"
            + (f"<br>Inherits: {_q(parent_id)}" if parent_id else "")
            + f"<br>Module: {_q(mod)}",
            group=mod,
            node_data={"type": "view", "xml_id": xml_id, "view_type": vtype,
                       "model": r.get("model", ""), "module": mod},
        )
        g.edge(vid, f"m:{r.get('model','')}", "for model", "view_model")
        if parent_id:
            g.edge(vid, f"v:{parent_id}", "inherits", "view_inherit", dashes=True)

    # ── State machine nodes ───────────────────────────────────────────────────
    for r in data["state_machines"]:
        mn = r["model_name"]
        field = r["field_name"]
        states = _jl(r.get("states"))
        transitions = _jl(r.get("transitions"))

        state_keys = []
        for s in states:
            key = s[0] if isinstance(s, (list, tuple)) else s
            label = (s[1] if isinstance(s, (list, tuple)) and len(s) > 1 else key) or key
            sid = f"st:{mn}.{field}.{key}"
            state_keys.append((key, sid))
            g.node(sid, label, "state",
                   f"<b>State: {_q(label)}</b><br>Key: {key}<br>Field: {_q(mn)}.{field}",
                   group=mn,
                   node_data={"type": "state", "model": mn, "field": field,
                               "key": key, "label": label})

        if state_keys:
            g.edge(f"m:{mn}", state_keys[0][1], field, "has_state", dashes=True)

        seen: set[tuple] = set()
        for t in transitions:
            frm = t.get("from") or t.get("from_state") or ""
            to = t.get("to") or t.get("to_state") or ""
            meth = t.get("method", "") or t.get("button", "") or ""
            if not to:
                continue
            frm_sid = f"st:{mn}.{field}.{frm}" if frm else None
            to_sid = f"st:{mn}.{field}.{to}"
            if to_sid not in g.nodes:
                g.node(to_sid, to, "state",
                       f"<b>State: {to}</b><br>Model: {_q(mn)}.{field}")
            key_t = (frm, to, meth)
            if key_t in seen:
                continue
            seen.add(key_t)
            if frm_sid and frm_sid in g.nodes:
                g.edge(frm_sid, to_sid, meth or "→", "state_trans", width=2.0)
            else:
                g.edge(f"m:{mn}", to_sid, meth or "→", "state_trans")

    # ── Action nodes ──────────────────────────────────────────────────────────
    for r in data["actions"]:
        xml_id = r.get("xml_id") or ""
        if not xml_id:
            continue
        aid = f"ac:{xml_id}"
        name = r.get("name") or xml_id
        mod = r.get("module_name") or ""
        mn = r.get("res_model") or ""
        g.node(
            aid, f"⚡ {name}", "action",
            f"<b>{_q(xml_id)}</b><br>Type: act_window<br>Model: {_q(mn)}<br>Module: {_q(mod)}",
            group=mod,
            node_data={"type": "action", "xml_id": xml_id, "name": name, "model": mn},
        )
        g.edge(aid, f"m:{mn}", "targets", "action_tgt")

    # ── Security nodes ────────────────────────────────────────────────────────
    for mn, acl_list in acl_by_model.items():
        if f"m:{mn}" not in g.nodes:
            continue
        sec_id = f"sec:{mn}"
        n_rules = len(rrule_by_model.get(mn, []))
        g.node(
            sec_id,
            f"🔐 ACL\n({len(acl_list)}+{n_rules})",
            "security",
            f"<b>Security: {_q(mn)}</b><br>ACL rules: {len(acl_list)}<br>Record rules: {n_rules}",
            group=mn,
            node_data={"type": "security", "model": mn,
                       "acl": acl_by_model.get(mn, []),
                       "record_rules": rrule_by_model.get(mn, [])},
        )
        g.edge(f"m:{mn}", sec_id, "security", "security", dashes=True)

    # ── Cron nodes ────────────────────────────────────────────────────────────
    for mn, cron_list in cron_by_model.items():
        if f"m:{mn}" not in g.nodes:
            continue
        cron_id = f"cron:{mn}"
        names = [c.get("name") or c.get("method_name", "") for c in cron_list[:3]]
        g.node(
            cron_id,
            f"⏰ Cron\n({len(cron_list)})",
            "cron",
            f"<b>Cron jobs: {_q(mn)}</b><br>" + "<br>".join(_q(n) for n in names),
            group=mn,
            node_data={"type": "cron", "model": mn, "jobs": cron_list},
        )
        g.edge(f"m:{mn}", cron_id, "cron", "cron", dashes=True)

    # ── Module dep edges ──────────────────────────────────────────────────────
    for r in data["module_deps"]:
        src_mod = r["module_name"]
        dep = r["depends_on"]
        if f"mod:{src_mod}" in g.nodes:
            g.node(f"mod:{dep}", dep, "module", f"<b>Module: {_q(dep)}</b>")
            g.edge(f"mod:{src_mod}", f"mod:{dep}", "depends on", "module_dep")


# ─────────────────────────────────────────────────────────────────────────────
# Full graph mode — all models
# ─────────────────────────────────────────────────────────────────────────────

def _build_full_graph(conn, g: GraphBuilder) -> None:
    """Build a full-codebase graph: all models with inherit + field relation edges only."""
    all_model_rows = [dict(r) for r in _rows(conn,
        "SELECT name, description, module_name, abstract FROM models WHERE inherit_type='primary' OR inherit_type IS NULL")]
    model_names = [r["name"] for r in all_model_rows]

    if not model_names:
        return

    ph, params = _in_clause(model_names)

    # Field / method counts
    fc = {r["model_name"]: r["cnt"] for r in _rows(conn,
        f"SELECT model_name, COUNT(*) AS cnt FROM fields WHERE model_name IN ({ph}) GROUP BY model_name", params)}
    mc = {r["model_name"]: r["cnt"] for r in _rows(conn,
        f"SELECT model_name, COUNT(*) AS cnt FROM methods WHERE model_name IN ({ph}) GROUP BY model_name", params)}

    for r in all_model_rows:
        mn = r["name"]
        mod = r.get("module_name") or ""
        desc = r.get("description") or ""
        abstract = bool(r.get("abstract"))
        n_f = fc.get(mn, 0)
        n_m = mc.get(mn, 0)
        g.node(
            f"m:{mn}", mn, "model",
            f"<b>{_q(mn)}</b>" + (f"<br><i>{_q(desc)}</i>" if desc else "")
            + f"<br>Module: {_q(mod)}<br>Fields: {n_f} · Methods: {n_m}"
            + (" <i>(abstract)</i>" if abstract else ""),
            group=mod,
        )

    # Inheritance edges
    for r in [dict(x) for x in _rows(conn,
        f"SELECT name, inherit_model, inherit_type FROM models WHERE name IN ({ph}) AND inherit_model IS NOT NULL AND inherit_model!=''",
        params)]:
        for parent in _parse_inherit(r["inherit_model"]):
            g.edge(f"m:{r['name']}", f"m:{parent}",
                   r.get("inherit_type") or "_inherit", "inherit", dashes=True)

    # Relational field edges (only between known models to avoid mega-graph clutter)
    _FT_SHORT2 = {"Many2one": "M2o", "One2many": "O2m", "Many2many": "M2m"}
    for r in [dict(x) for x in _rows(conn,
        f"""SELECT model_name, field_name, field_type, comodel_name
            FROM fields
            WHERE model_name IN ({ph})
              AND field_type IN ('Many2one','One2many','Many2many')
              AND comodel_name IN ({ph})
            ORDER BY model_name, field_type""",
        params + params)]:
        short = _FT_SHORT2.get(r["field_type"], r["field_type"][:3])
        g.edge(f"m:{r['model_name']}", f"m:{r['comodel_name']}",
               f"{r['field_name']} ({short})", "field_rel")


# ─────────────────────────────────────────────────────────────────────────────
# Module-centric graph
# ─────────────────────────────────────────────────────────────────────────────

def _build_module_graph(conn, g: GraphBuilder, module_name: str, depth: int) -> None:
    """Build a module-centric graph."""
    visited_mods: set[str] = set()
    model_names_set: set[str] = set()

    def _add_module(mod: str, d: int, is_root: bool = False) -> None:
        if mod in visited_mods:
            return
        visited_mods.add(mod)
        row = conn.execute("SELECT category, application FROM modules WHERE name=?", (mod,)).fetchone()
        cat = (row["category"] or "") if row else ""
        is_app = bool(row["application"]) if row else False
        g.node(
            f"mod:{mod}", mod, "module",
            f"<b>Module: {_q(mod)}</b>" + (f"<br>Category: {_q(cat)}" if cat else "")
            + (" · app" if is_app else ""),
            is_root=is_root,
        )
        model_rows = _rows(conn,
            "SELECT name FROM models WHERE module_name=? AND (inherit_type='primary' OR inherit_type IS NULL)", (mod,))
        for mr in model_rows:
            model_names_set.add(mr["name"])
        if d <= 0:
            return
        for dr in _rows(conn, "SELECT depends_on FROM module_deps WHERE module_name=?", (mod,)):
            dep = dr["depends_on"]
            g.node(f"mod:{dep}", dep, "module", f"<b>Module: {_q(dep)}</b>")
            g.edge(f"mod:{mod}", f"mod:{dep}", "depends on", "module_dep")
            if d > 1:
                _add_module(dep, d - 1)

    _add_module(module_name, depth, is_root=True)

    if not model_names_set:
        return

    # Expand all collected models via batch fetch
    model_list = list(model_names_set)
    # Also grab related models within depth=1
    related: set[str] = set(model_list)
    for mn in model_list:
        for r in _rows(conn, """
            SELECT DISTINCT comodel_name FROM fields
            WHERE model_name=? AND field_type IN ('Many2one','One2many','Many2many')
            AND comodel_name IS NOT NULL AND comodel_name != ''""", (mn,)):
            related.add(r["comodel_name"])

    batch = _batch_fetch(conn, list(related))
    _build_graph(g, batch, root_model=None)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_graph_html(
    db_path: Path,
    model_name: Optional[str] = None,
    module_name: Optional[str] = None,
    depth: int = 2,
    all_models: bool = False,
) -> tuple[str, str]:
    """Generate interactive HTML knowledge graph.

    Args:
        db_path:     Path to the SQLite index.
        model_name:  Centre on a specific model.
        module_name: Centre on a module.
        depth:       How many hops to expand from the root.
        all_models:  If True, render all models in the codebase (inherit + field edges only).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Index not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        g = GraphBuilder()

        if all_models:
            _build_full_graph(conn, g)
            title = "Full Codebase Graph"
        elif module_name:
            _build_module_graph(conn, g, module_name, depth)
            title = f"Module: {module_name} (depth {depth})"
        elif model_name:
            # BFS collect all relevant model names
            all_model_names = _collect_models_bfs(conn, model_name, depth)
            batch = _batch_fetch(conn, list(all_model_names))
            _build_graph(g, batch, root_model=model_name)
            title = f"Model: {model_name} (depth {depth})"
        else:
            raise ValueError("Provide model_name, module_name, or all_models=True")

        data = g.to_dict(title)
        return _render_html(data), title

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HTML renderer
# ─────────────────────────────────────────────────────────────────────────────

def _render_html(data: dict) -> str:
    _known_types = {"model", "method", "view", "state", "action", "module", "security", "cron"}
    filter_types = [t for t in _known_types
                    if any(n.get("group") == t for n in data["nodes"])]

    type_labels = {
        "model":    ("Models",    "#3b82f6"),
        "method":   ("Methods",   "#8b5cf6"),
        "view":     ("Views",     "#f97316"),
        "state":    ("States",    "#22c55e"),
        "action":   ("Actions",   "#ec4899"),
        "module":   ("Modules",   "#16a34a"),
        "security": ("Security",  "#ca8a04"),
        "cron":     ("Cron",      "#0284c7"),
    }

    edge_type_labels = {
        "inherit":      ("Inheritance",    "#6366f1"),
        "field_rel":    ("Field relations","#3b82f6"),
        "compute":      ("Compute",        "#8b5cf6"),
        "constrains":   ("Constrains",     "#f43f5e"),
        "onchange":     ("Onchange",       "#f97316"),
        "action_meth":  ("Action methods", "#ec4899"),
        "view_model":   ("View → Model",   "#fb923c"),
        "view_inherit": ("View inherit",   "#fed7aa"),
        "state_trans":  ("State trans.",   "#22c55e"),
        "has_state":    ("State machine",  "#86efac"),
        "action_tgt":   ("Action targets", "#f472b6"),
        "module_dep":   ("Module dep",     "#94a3b8"),
        "defined_in":   ("Defined in",     "#64748b"),
        "security":     ("Security",       "#ca8a04"),
        "cron":         ("Cron",           "#0284c7"),
    }

    present_edge_types = sorted({e.get("edgeType", "") for e in data["edges"] if e.get("edgeType")})

    filter_checkboxes = "\n".join(
        f'<label class="filter-item" style="border-left:3px solid {col}">'
        f'<input type="checkbox" checked data-ntype="{t}" onchange="toggleNodeType(this)"> {lbl}</label>'
        for t, (lbl, col) in type_labels.items()
        if t in filter_types
    )

    edge_checkboxes = "\n".join(
        f'<label class="filter-item" style="border-left:3px solid {col}">'
        f'<input type="checkbox" checked data-etype="{t}" onchange="toggleEdgeType(this)"> {lbl}</label>'
        for t, (lbl, col) in edge_type_labels.items()
        if t in present_edge_types
    )

    n_nodes = len(data["nodes"])
    n_edges = len(data["edges"])
    stats = f"{n_nodes} nodes · {n_edges} edges"

    # Large-graph hint: tune physics but ALWAYS use force layout.
    # Hierarchical layout breaks on Odoo graphs (they always have cycles).
    large_graph = n_nodes > 120

    node_data_json = json.dumps(data.get("node_data", {}), ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{data['title']}</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;height:100vh;display:flex;flex-direction:column;overflow:hidden}}
  header{{padding:8px 14px;background:#1e293b;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px;flex-shrink:0;min-height:44px}}
  header h1{{font-size:13px;font-weight:600;color:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:380px}}
  .badge{{font-size:10px;padding:2px 7px;border-radius:99px;background:#334155;color:#94a3b8;white-space:nowrap}}
  .stats{{font-size:10px;color:#64748b;white-space:nowrap}}
  .controls{{display:flex;gap:5px;margin-left:auto;align-items:center;flex-shrink:0;flex-wrap:wrap}}
  .btn{{font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid #475569;background:#1e293b;color:#cbd5e1;cursor:pointer;white-space:nowrap;line-height:1.4}}
  .btn:hover{{background:#334155}}
  .btn.active{{background:#4f46e5;border-color:#6366f1;color:#fff}}
  .btn-zoom{{padding:4px 8px;font-size:14px;font-weight:bold}}
  select.btn{{padding:3px 6px}}
  .main{{display:flex;flex:1;overflow:hidden}}
  #sidebar{{width:200px;flex-shrink:0;background:#1e293b;border-right:1px solid #334155;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:12px}}
  .sidebar-section h4{{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}}
  .filter-item{{display:flex;align-items:center;gap:6px;font-size:11px;color:#cbd5e1;padding:3px 7px;border-radius:4px;cursor:pointer;margin-bottom:1px}}
  .filter-item:hover{{background:#334155}}
  .filter-item input{{accent-color:#6366f1;cursor:pointer;flex-shrink:0}}
  #graph{{flex:1;position:relative;overflow:hidden}}
  #info-panel{{position:absolute;right:10px;top:10px;width:260px;background:#1e293b;border:1px solid #334155;border-radius:8px;padding:11px;font-size:11px;line-height:1.6;display:none;z-index:10;box-shadow:0 4px 24px rgba(0,0,0,.6);max-height:70vh;overflow-y:auto}}
  #info-panel h3{{font-size:12px;color:#f8fafc;margin-bottom:6px;border-bottom:1px solid #334155;padding-bottom:5px;word-break:break-all}}
  #info-panel .info-section{{margin-top:8px}}
  #info-panel .info-section h4{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}}
  #info-panel table{{width:100%;border-collapse:collapse;font-size:10px}}
  #info-panel td,#info-panel th{{padding:2px 5px;border-bottom:1px solid #1e3a5f;text-align:left;vertical-align:top}}
  #info-panel th{{color:#64748b;font-weight:500}}
  #info-panel .kv{{display:flex;gap:6px;margin-bottom:2px}}
  #info-panel .kv .k{{color:#64748b;min-width:60px;flex-shrink:0}}
  #info-panel .kv .v{{color:#e2e8f0;word-break:break-all}}
  #info-panel .tag{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:9px;margin:1px;background:#334155;color:#94a3b8}}
  #info-panel .tag.green{{background:#052e16;color:#86efac}}
  #info-panel .tag.yellow{{background:#422006;color:#fde047}}
  #info-panel .tag.blue{{background:#0c1e3c;color:#93c5fd}}
  .info-close{{float:right;cursor:pointer;color:#64748b;font-size:14px;line-height:1;margin-top:-2px;padding:0 2px}}
  .info-close:hover{{color:#e2e8f0}}
  #search{{width:100%;padding:5px 8px;border-radius:5px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:11px;outline:none}}
  #search:focus{{border-color:#6366f1}}
  .search-results{{margin-top:3px;max-height:110px;overflow-y:auto}}
  .sri{{font-size:10px;padding:3px 6px;border-radius:3px;cursor:pointer;color:#94a3b8}}
  .sri:hover{{background:#334155;color:#e2e8f0}}
  #toggle-sb{{position:absolute;left:200px;top:50%;transform:translateY(-50%);z-index:20;background:#1e293b;border:1px solid #334155;border-left:none;padding:4px 3px;border-radius:0 4px 4px 0;cursor:pointer;font-size:10px;color:#64748b;transition:left .15s}}
  .legend-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;vertical-align:middle}}
  #large-graph-note{{position:absolute;bottom:10px;right:10px;background:#1e293b;border:1px solid #334155;border-radius:6px;padding:6px 10px;font-size:10px;color:#94a3b8;z-index:5}}
</style>
</head>
<body>
<header>
  <h1>{data['title']}</h1>
  <span class="badge">Knowledge Graph</span>
  <span class="stats" id="stats-badge">{stats}</span>
  <div class="controls">
    <select class="btn" id="layoutSel" onchange="applyLayout(this.value)">
      <option value="force" selected>Force</option>
      <option value="hier_ud">Hierarchical ↓</option>
      <option value="hier_lr">Hierarchical →</option>
    </select>
    <button class="btn btn-zoom" onclick="zoom(1.3)" title="Zoom in">＋</button>
    <button class="btn btn-zoom" onclick="zoom(0.77)" title="Zoom out">－</button>
    <button class="btn" onclick="network.fit({{animation:true}})">Fit all</button>
    <button class="btn" id="physBtn" onclick="togglePhysics()">Pause</button>
    <button class="btn" id="clusterBtn" onclick="toggleDetails()">Compact</button>
    <button class="btn" onclick="exportPng()">PNG</button>
    <button class="btn" onclick="resetHighlight()">Reset</button>
  </div>
</header>
<div class="main">
  <div id="sidebar">
    <div class="sidebar-section">
      <h4>Search</h4>
      <input type="text" id="search" placeholder="Filter nodes…" oninput="searchNodes(this.value)">
      <div class="search-results" id="searchResults"></div>
    </div>
    <div class="sidebar-section">
      <h4>Node types</h4>
      {filter_checkboxes}
    </div>
    <div class="sidebar-section">
      <h4>Edge types</h4>
      {edge_checkboxes}
    </div>
    <div class="sidebar-section">
      <h4>Legend</h4>
      <div style="font-size:10px;color:#64748b;line-height:2">
        <div><span class="legend-dot" style="background:#dbeafe;border:2px solid #3b82f6"></span>Model</div>
        <div><span class="legend-dot" style="background:#ede9fe;border:2px solid #8b5cf6"></span>Method</div>
        <div><span class="legend-dot" style="background:#fff7ed;border:2px solid #f97316"></span>View</div>
        <div><span class="legend-dot" style="background:#dcfce7;border:2px solid #22c55e"></span>State</div>
        <div><span class="legend-dot" style="background:#fce7f3;border:2px solid #ec4899"></span>Action</div>
        <div><span class="legend-dot" style="background:#f0fdf4;border:2px solid #16a34a"></span>Module</div>
        <div><span class="legend-dot" style="background:#fef9c3;border:2px solid #ca8a04"></span>Security</div>
        <div><span class="legend-dot" style="background:#e0f2fe;border:2px solid #0284c7"></span>Cron</div>
      </div>
    </div>
  </div>
  <div id="graph">
    <div id="toggle-sb" onclick="toggleSidebar()">◀</div>
    <div id="info-panel">
      <span class="info-close" onclick="closeInfo()">✕</span>
      <h3 id="info-title">Node Info</h3>
      <div id="info-body"></div>
    </div>
    {('<div id="large-graph-note">⏳ Stabilizing layout…</div>' if large_graph else '')}
  </div>
</div>

<script>
// ── Data ─────────────────────────────────────────────────────────────────────
const ALL_NODES = {json.dumps(data['nodes'], ensure_ascii=False)};
const ALL_EDGES = {json.dumps(data['edges'], ensure_ascii=False)};
const NODE_DATA = {node_data_json};

// ── DataSets ──────────────────────────────────────────────────────────────────
const nodesDS = new vis.DataSet(ALL_NODES.map(n => ({{...n}})));
const edgesDS = new vis.DataSet(ALL_EDGES.map(e => ({{...e}})));

// Quick node group lookup (id → group)
const nodeGroupMap = {{}};
ALL_NODES.forEach(n => nodeGroupMap[n.id] = n.group);

// ── Filter state ──────────────────────────────────────────────────────────────
const hiddenNodeTypes = new Set();
const hiddenEdgeTypes = new Set();

// Detail mode: hide method/view/state/security/cron nodes
let detailHidden = false;
const DETAIL_TYPES = new Set(['method','view','state','security','cron']);

// ── DataView with filter functions ────────────────────────────────────────────
const nodesView = new vis.DataView(nodesDS, {{
  filter: n => {{
    if (hiddenNodeTypes.has(n.group)) return false;
    if (detailHidden && DETAIL_TYPES.has(n.group)) return false;
    return true;
  }}
}});
const edgesView = new vis.DataView(edgesDS, {{
  filter: e => {{
    if (hiddenEdgeTypes.has(e.edgeType || '')) return false;
    const fg = nodeGroupMap[e.from] || '';
    const tg = nodeGroupMap[e.to] || '';
    if (hiddenNodeTypes.has(fg) || hiddenNodeTypes.has(tg)) return false;
    if (detailHidden && (DETAIL_TYPES.has(fg) || DETAIL_TYPES.has(tg))) return false;
    return true;
  }}
}});

// ── vis.js options ────────────────────────────────────────────────────────────
// Always force-directed layout — hierarchical breaks on cyclic graphs (Odoo always has cycles).
// For large graphs, tune physics to stabilize faster then auto-stop.
const LARGE = {json.dumps(large_graph)};
const OPTIONS = {{
  nodes:{{margin:{{top:5,bottom:5,left:8,right:8}},shadow:false}},
  edges:{{
    font:{{size:9,color:'#94a3b8',background:'rgba(15,23,42,0.8)',align:'middle'}},
    smooth:{{enabled:true,type:'dynamic'}},selectionWidth:3,hoverWidth:2,
  }},
  physics:{{
    enabled: true,
    stabilization:{{
      iterations: LARGE ? 600 : 300,
      updateInterval: LARGE ? 25 : 50,
      fit: true,
    }},
    barnesHut:{{
      gravitationalConstant: LARGE ? -18000 : -12000,
      centralGravity: LARGE ? 0.5 : 0.2,
      springLength: LARGE ? 100 : 160,
      springConstant: LARGE ? 0.05 : 0.025,
      damping: LARGE ? 0.18 : 0.09,
      avoidOverlap: LARGE ? 0.4 : 0.15,
    }},
  }},
  interaction:{{hover:true,tooltipDelay:200,zoomSpeed:0.8,multiselect:true,navigationButtons:false}},
  layout:{{randomSeed:42}},
}};

const container = document.getElementById('graph');
const network = new vis.Network(container, {{nodes: nodesView, edges: edgesView}}, OPTIONS);

let _physics = true;

// ── Info panel ────────────────────────────────────────────────────────────────
const infoPanel = document.getElementById('info-panel');

function _permsTag(r) {{
  const p = (r.perm_read?'R':'') + (r.perm_write?'W':'') + (r.perm_create?'C':'') + (r.perm_unlink?'D':'');
  return `<span class="tag">${{p||'none'}}</span>`;
}}

function _buildInfoHtml(nid, node) {{
  const nd = NODE_DATA[nid];
  if (!nd) return `<div>${{node.title||''}}</div>`;
  const t = nd.type;
  let html = '';

  if (t === 'model') {{
    html += `<div class="kv"><span class="k">Module</span><span class="v">${{nd.module||'—'}}</span></div>`;
    if (nd.description) html += `<div class="kv"><span class="k">Desc</span><span class="v">${{nd.description}}</span></div>`;
    html += `<div class="kv"><span class="k">Fields</span><span class="v">${{nd.field_count}}</span></div>`;
    html += `<div class="kv"><span class="k">Methods</span><span class="v">${{nd.method_count}}</span></div>`;
    if (nd.abstract) html += `<span class="tag yellow">abstract</span>`;
    if (nd.transient) html += `<span class="tag blue">transient</span>`;

    if (nd.acl && nd.acl.length) {{
      html += `<div class="info-section"><h4>Access Rules (ACL)</h4>`;
      html += `<table><tr><th>Name</th><th>Group</th><th>Perms</th></tr>`;
      nd.acl.forEach(r => {{
        html += `<tr><td>${{r.name}}</td><td style="color:#64748b">${{r.group}}</td><td>${{r.perms}}</td></tr>`;
      }});
      html += `</table></div>`;
    }}
    if (nd.record_rules && nd.record_rules.length) {{
      html += `<div class="info-section"><h4>Record Rules</h4>`;
      nd.record_rules.forEach(r => {{
        html += `<div class="kv"><span class="k" style="max-width:70px;overflow:hidden;text-overflow:ellipsis">${{r.name}}</span>`;
        html += `<span class="v" style="color:#64748b;font-size:9px">${{r.domain}}</span></div>`;
      }});
      html += `</div>`;
    }}
    if (nd.cron && nd.cron.length) {{
      html += `<div class="info-section"><h4>Cron Jobs</h4>`;
      nd.cron.forEach(c => {{
        html += `<div class="kv"><span class="k">${{c.method}}</span><span class="v">${{c.interval}}</span></div>`;
      }});
      html += `</div>`;
    }}
  }} else if (t === 'method') {{
    html += `<div class="kv"><span class="k">Model</span><span class="v">${{nd.model}}</span></div>`;
    html += `<div class="kv"><span class="k">Type</span><span class="v">${{nd.subtype}}</span></div>`;
    if (nd.field) html += `<div class="kv"><span class="k">Field</span><span class="v">${{nd.field}}</span></div>`;
    if (nd.depends && nd.depends.length) {{
      html += `<div class="info-section"><h4>@depends</h4>`;
      nd.depends.forEach(f => html += `<span class="tag green">${{f}}</span>`);
      html += `</div>`;
    }}
    if (nd.fields && nd.fields.length) {{
      html += `<div class="info-section"><h4>Fields</h4>`;
      nd.fields.forEach(f => html += `<span class="tag green">${{f}}</span>`);
      html += `</div>`;
    }}
    if (nd.transitions && nd.transitions.length) {{
      html += `<div class="info-section"><h4>State Transitions</h4>`;
      nd.transitions.slice(0,5).forEach(t2 => {{
        const fr = t2.from||'*'; const to = t2.to||'?';
        html += `<div class="kv"><span class="k">${{fr}} → ${{to}}</span>`;
        if (t2.method||t2.button) html += `<span class="v" style="color:#64748b">${{t2.method||t2.button}}</span>`;
        html += `</div>`;
      }});
      html += `</div>`;
    }}
  }} else if (t === 'view') {{
    html += `<div class="kv"><span class="k">Type</span><span class="v">${{nd.view_type}}</span></div>`;
    html += `<div class="kv"><span class="k">Model</span><span class="v">${{nd.model}}</span></div>`;
    html += `<div class="kv"><span class="k">Module</span><span class="v">${{nd.module}}</span></div>`;
  }} else if (t === 'security') {{
    if (nd.acl && nd.acl.length) {{
      html += `<div class="info-section"><h4>Access Rules</h4>`;
      html += `<table><tr><th>Name</th><th>Group</th><th>RWCD</th></tr>`;
      nd.acl.forEach(r => {{
        const g2 = (r.group_xml_id||'all users').split('.').pop();
        const perms = [(r.perm_read?'R':'_'),(r.perm_write?'W':'_'),(r.perm_create?'C':'_'),(r.perm_unlink?'D':'_')].join('');
        html += `<tr><td>${{r.name}}</td><td style="color:#64748b">${{g2}}</td><td>${{perms}}</td></tr>`;
      }});
      html += `</table></div>`;
    }}
    if (nd.record_rules && nd.record_rules.length) {{
      html += `<div class="info-section"><h4>Record Rules</h4>`;
      nd.record_rules.forEach(r => {{
        html += `<div class="kv"><span class="k">${{r.name}}</span><span class="v" style="color:#64748b;font-size:9px">${{(r.domain_force||'').slice(0,80)}}</span></div>`;
      }});
      html += `</div>`;
    }}
  }} else if (t === 'cron') {{
    html += `<div class="info-section"><h4>Scheduled Jobs</h4>`;
    html += `<table><tr><th>Name</th><th>Method</th><th>Interval</th><th>Active</th></tr>`;
    (nd.jobs||[]).forEach(j => {{
      html += `<tr><td>${{(j.name||j.xml_id||'').split('.').pop()}}</td><td style="color:#93c5fd">${{j.method_name||''}}</td>`;
      html += `<td>${{j.interval_number}} ${{j.interval_type}}</td><td>${{j.active?'✓':'✗'}}</td></tr>`;
    }});
    html += `</table></div>`;
  }} else if (t === 'state') {{
    html += `<div class="kv"><span class="k">Model</span><span class="v">${{nd.model}}</span></div>`;
    html += `<div class="kv"><span class="k">Field</span><span class="v">${{nd.field}}</span></div>`;
    html += `<div class="kv"><span class="k">Key</span><span class="v">${{nd.key}}</span></div>`;
  }} else if (t === 'action') {{
    html += `<div class="kv"><span class="k">Model</span><span class="v">${{nd.model}}</span></div>`;
    html += `<div class="kv"><span class="k">ID</span><span class="v" style="font-size:9px">${{nd.xml_id}}</span></div>`;
  }}
  return html;
}}

network.on('click', params => {{
  if (params.nodes.length === 0) {{
    closeInfo();
    resetHighlight();
    return;
  }}
  const nid = params.nodes[0];
  const node = nodesDS.get(nid);
  if (!node) return;

  // Highlight neighbourhood (only visible nodes)
  const connected = new Set(network.getConnectedNodes(nid));
  const connEdges = new Set(network.getConnectedEdges(nid));

  const visNodeIds = nodesView.getIds();
  nodesDS.update(visNodeIds.map(id => ({{id, opacity: (id===nid || connected.has(id)) ? 1 : 0.15}})));
  const visEdgeIds = edgesView.getIds();
  edgesDS.update(visEdgeIds.map(id => ({{
    id,
    opacity: connEdges.has(id) ? 1 : 0.05,
    width: connEdges.has(id) ? 2.5 : 1.0,
  }})));

  // Info panel
  document.getElementById('info-title').textContent = node.label.replace(/\\n/g,' ');
  document.getElementById('info-body').innerHTML = _buildInfoHtml(nid, node);
  infoPanel.style.display = 'block';
}});

function closeInfo() {{
  infoPanel.style.display = 'none';
}}
function resetHighlight() {{
  const visNodeIds = nodesView.getIds();
  nodesDS.update(visNodeIds.map(id => ({{id, opacity:1}})));
  const visEdgeIds = edgesView.getIds();
  edgesDS.update(visEdgeIds.map(id => {{
    const e = edgesDS.get(id);
    return {{id, opacity:0.85, width: e._w || 1.5}};
  }}));
  infoPanel.style.display = 'none';
}}

// ── Filters (DataView.refresh() — O(visible) not O(total)) ──────────────────
function toggleNodeType(cb) {{
  const t = cb.dataset.ntype;
  if (cb.checked) hiddenNodeTypes.delete(t); else hiddenNodeTypes.add(t);
  nodesView.refresh();
  edgesView.refresh();
}}
function toggleEdgeType(cb) {{
  const t = cb.dataset.etype;
  if (cb.checked) hiddenEdgeTypes.delete(t); else hiddenEdgeTypes.add(t);
  edgesView.refresh();
}}

// ── Compact/detail toggle ─────────────────────────────────────────────────────
function toggleDetails() {{
  detailHidden = !detailHidden;
  document.getElementById('clusterBtn').classList.toggle('active', detailHidden);
  nodesView.refresh();
  edgesView.refresh();
}}

// ── Search ────────────────────────────────────────────────────────────────────
function searchNodes(q) {{
  const box = document.getElementById('searchResults');
  q = q.trim().toLowerCase();
  if (!q) {{ box.innerHTML=''; return; }}
  const hits = ALL_NODES.filter(n =>
    (n.label||'').toLowerCase().includes(q) || (n.id||'').toLowerCase().includes(q)
  ).slice(0,14);
  box.innerHTML = hits.map(n =>
    `<div class="sri" onclick="focusNode('${{n.id.replace(/'/g,"\\\\'")}}')">` +
    n.label.replace(/\\n/g,' ') + `</div>`
  ).join('');
}}
function focusNode(id) {{
  network.focus(id, {{scale:1.5, animation:true}});
  network.selectNodes([id]);
  document.getElementById('searchResults').innerHTML='';
  document.getElementById('search').value='';
}}

// ── Physics ───────────────────────────────────────────────────────────────────
function togglePhysics() {{
  _physics = !_physics;
  network.setOptions({{physics:{{enabled:_physics}}}});
  document.getElementById('physBtn').textContent = _physics ? 'Pause' : 'Resume';
}}

// ── Layout ────────────────────────────────────────────────────────────────────
function applyLayout(v) {{
  if (v==='force') {{
    network.setOptions({{layout:{{hierarchical:{{enabled:false}}}},physics:{{enabled:true}}}});
    _physics=true;
    document.getElementById('physBtn').textContent='Pause';
  }} else {{
    const dir = v==='hier_ud' ? 'UD' : 'LR';
    network.setOptions({{
      layout:{{hierarchical:{{enabled:true,direction:dir,sortMethod:'directed',levelSeparation:130,nodeSpacing:70}}}},
      physics:{{enabled:false}},
    }});
    _physics=false;
    document.getElementById('physBtn').textContent='Resume';
    setTimeout(()=>network.fit({{animation:true}}),250);
  }}
}}

// ── Zoom ──────────────────────────────────────────────────────────────────────
function zoom(factor) {{
  const scale = network.getScale() * factor;
  const pos = network.getViewPosition();
  network.moveTo({{scale, position:pos, animation:{{duration:200,easingFunction:'easeInOutQuad'}}}});
}}

// ── PNG export ────────────────────────────────────────────────────────────────
function exportPng() {{
  const canvas = container.querySelector('canvas');
  if (!canvas) return;
  const a = document.createElement('a');
  a.href = canvas.toDataURL('image/png');
  a.download = '{data['title'].replace(" ", "_")}.png';
  a.click();
}}

// ── Sidebar ───────────────────────────────────────────────────────────────────
let _sbOpen = true;
function toggleSidebar() {{
  const sb = document.getElementById('sidebar');
  const btn = document.getElementById('toggle-sb');
  _sbOpen = !_sbOpen;
  sb.style.display = _sbOpen ? '' : 'none';
  btn.style.left = _sbOpen ? '200px' : '0';
  btn.textContent = _sbOpen ? '◀' : '▶';
  network.redraw();
}}

// ── Auto-stabilize ────────────────────────────────────────────────────────────
if (LARGE) {{
  const note = document.getElementById('large-graph-note');
  network.on('stabilizationProgress', params => {{
    const pct = Math.round(params.iterations / params.total * 100);
    if (note) note.textContent = `⏳ Stabilizing… ${{pct}}%`;
  }});
}}

network.once('stabilizationIterationsDone', () => {{
  network.fit({{animation:{{duration:600}}}});
  const note = document.getElementById('large-graph-note');
  if (note) note.textContent = '⚡ Stabilized. Use Fit all or zoom to explore.';
  // Auto-pause physics after layout settles (keeps the nice cluster look)
  const delay = LARGE ? 1500 : 2500;
  setTimeout(() => {{
    network.setOptions({{physics:{{enabled:false}}}});
    _physics = false;
    document.getElementById('physBtn').textContent = 'Resume';
  }}, delay);
}});
</script>
</body>
</html>"""
