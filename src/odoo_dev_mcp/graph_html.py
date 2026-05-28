"""Comprehensive knowledge graph visualizer.

Extracts ALL inter-component relationships from the SQLite index and renders
them as an interactive vis.js HTML page:

  model ──_inherit──▶ model
  model ──field_rel──▶ model     (Many2one / One2many / Many2many)
  model ──compute──▶  method     (field computed by method)
  model ──action──▶   method     (action / constrains / onchange)
  view  ──for_model──▶ model
  view  ──inherits──▶  view
  state ──transition──▶ state
  model ──has_state──▶  state    (first state in machine)
  action──targets──▶   model
  module──depends──▶   module
  model ──defined_in──▶ module
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
}

_EDGE_COLOR: dict[str, str] = {
    "inherit":     "#6366f1",   # indigo - inheritance
    "field_rel":   "#3b82f6",   # blue   - Many2one/O2m/M2m
    "compute":     "#8b5cf6",   # violet - field compute
    "constrains":  "#f43f5e",   # rose   - @api.constrains
    "onchange":    "#f97316",   # orange - @api.onchange
    "action_meth": "#ec4899",   # pink   - action method
    "view_model":  "#fb923c",   # orange - view → model
    "view_inherit":"#fed7aa",   # light  - view inherit
    "state_trans": "#22c55e",   # green  - state transition
    "has_state":   "#86efac",   # light  - model → state
    "action_tgt":  "#f472b6",   # pink   - action targets model
    "module_dep":  "#94a3b8",   # gray   - module dependency
    "defined_in":  "#64748b",   # dark   - model → module
}


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────

class GraphBuilder:
    """Incrementally builds nodes + edges, deduplicating both."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}   # id → vis node
        self.edges: list[dict] = []
        self._edge_keys: set[tuple] = set()
        self._eid = 0

    # ── node ──────────────────────────────────────────────────────────────────

    def node(
        self,
        node_id: str,
        label: str,
        node_type: str,
        title: str = "",
        is_root: bool = False,
        group: str = "",
    ) -> None:
        if node_id in self.nodes:
            return
        style = dict(_NODE_STYLE.get(node_type, _NODE_STYLE["model"]))
        # Deep-copy color dict to avoid mutation across nodes
        style["color"] = dict(style["color"])
        style["color"]["highlight"] = dict(style["color"].get("highlight", {}))
        style["font"] = dict(style.get("font", {}))

        if is_root:
            style["borderWidth"] = 4
            style["font"] = {**style["font"], "bold": True, "size": 15}
            style["color"] = {
                **style["color"],
                "background": "#c7d2fe",
                "border": "#4f46e5",
            }

        self.nodes[node_id] = {
            "id": node_id,
            "label": label,
            "group": group or node_type,
            "title": title or label,
            **style,
        }

    # ── edge ──────────────────────────────────────────────────────────────────

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
        self.edges.append(
            {
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
                "edgeType": edge_type,  # custom field for JS filter
            }
        )

    # ── export ────────────────────────────────────────────────────────────────

    def to_dict(self, title: str = "Odoo Knowledge Graph") -> dict:
        return {
            "title": title,
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Data extractors
# ─────────────────────────────────────────────────────────────────────────────

def _q(s: str) -> str:
    return (s or "").replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")


def _rows(conn, sql: str, params=()) -> list:
    return conn.execute(sql, params).fetchall()


def _extract_model(
    conn,
    g: GraphBuilder,
    model_name: str,
    depth: int,
    visited_models: set,
    is_root: bool = False,
) -> None:
    """Recursively add a model and all its connections up to `depth` hops."""
    if model_name in visited_models:
        return
    visited_models.add(model_name)

    # ── Model node ────────────────────────────────────────────────────────────
    model_row = conn.execute(
        "SELECT description, module_name, abstract, transient FROM models WHERE name = ? LIMIT 1",
        (model_name,),
    ).fetchone()
    if not model_row and not is_root:
        return

    mod = (model_row["module_name"] or "") if model_row else ""
    desc = (model_row["description"] or "") if model_row else ""
    abstract = bool(model_row["abstract"]) if model_row else False

    field_count = conn.execute(
        "SELECT COUNT(*) FROM fields WHERE model_name=?", (model_name,)
    ).fetchone()[0]
    method_count = conn.execute(
        "SELECT COUNT(*) FROM methods WHERE model_name=?", (model_name,)
    ).fetchone()[0]

    tooltip = (
        f"<b>{_q(model_name)}</b>"
        + (f"<br><i>{_q(desc)}</i>" if desc else "")
        + f"<br>Module: <b>{_q(mod)}</b>"
        + f"<br>Fields: {field_count} · Methods: {method_count}"
        + (" <i>(abstract)</i>" if abstract else "")
    )
    g.node(f"m:{model_name}", model_name, "model", tooltip, is_root=is_root, group=mod)

    # ── Module node + defined_in edge ─────────────────────────────────────────
    if mod:
        g.node(f"mod:{mod}", mod, "module", f"<b>Module: {_q(mod)}</b>")
        g.edge(f"m:{model_name}", f"mod:{mod}", "defined in", "defined_in", dashes=True)

    # ── Inheritance ───────────────────────────────────────────────────────────
    for r in _rows(conn,
        "SELECT name, inherit_model, inherit_type, module_name FROM models WHERE name=? AND inherit_model IS NOT NULL",
        (model_name,)):
        parent = r["inherit_model"]
        g.edge(f"m:{model_name}", f"m:{parent}", r["inherit_type"] or "_inherit", "inherit", dashes=True)
        if depth > 1 and parent not in visited_models:
            _extract_model(conn, g, parent, depth - 1, visited_models)

    for r in _rows(conn,
        "SELECT name FROM models WHERE inherit_model=? AND inherit_type='_inherit'",
        (model_name,)):
        child = r["name"]
        g.edge(f"m:{child}", f"m:{model_name}", "_inherit", "inherit", dashes=True)
        if depth > 1 and child not in visited_models:
            _extract_model(conn, g, child, depth - 1, visited_models)

    # ── Relational fields → connected models ──────────────────────────────────
    for r in _rows(conn,
        """
        SELECT field_name, field_type, comodel_name, string_label
        FROM fields
        WHERE model_name=? AND field_type IN ('Many2one','One2many','Many2many','Many2oneReference')
          AND comodel_name IS NOT NULL AND comodel_name != ''
        ORDER BY field_type, field_name
        """,
        (model_name,)):
        target = r["comodel_name"]
        fname = r["field_name"]
        ftype = r["field_type"]
        short = {"Many2one": "M2o", "One2many": "O2m", "Many2many": "M2m"}.get(ftype, ftype[:3])
        g.edge(f"m:{model_name}", f"m:{target}", f"{fname} ({short})", "field_rel")
        if depth > 1 and target not in visited_models:
            _extract_model(conn, g, target, depth - 1, visited_models)
        elif target not in visited_models:
            # Add target as a stub node even at depth 0
            visited_models.add(target)
            _stub_model(conn, g, target)

    # ── Methods ───────────────────────────────────────────────────────────────
    _extract_methods(conn, g, model_name)

    # ── Views ─────────────────────────────────────────────────────────────────
    _extract_views(conn, g, model_name)

    # ── State machine ─────────────────────────────────────────────────────────
    _extract_states(conn, g, model_name)

    # ── Actions ───────────────────────────────────────────────────────────────
    _extract_actions(conn, g, model_name)


def _stub_model(conn, g: GraphBuilder, model_name: str) -> None:
    """Add a minimal model node (no sub-graph expansion)."""
    row = conn.execute(
        "SELECT description, module_name FROM models WHERE name=? LIMIT 1", (model_name,)
    ).fetchone()
    mod = (row["module_name"] or "") if row else ""
    desc = (row["description"] or "") if row else ""
    g.node(
        f"m:{model_name}", model_name, "model",
        f"<b>{_q(model_name)}</b>" + (f"<br><i>{_q(desc)}</i>" if desc else "") + f"<br>Module: {_q(mod)}",
        group=mod,
    )


def _extract_methods(conn, g: GraphBuilder, model_name: str) -> None:
    """Add method nodes for compute / constrains / onchange / action methods."""
    # Compute fields → method edges
    for r in _rows(conn,
        "SELECT field_name, field_type, compute FROM fields WHERE model_name=? AND compute IS NOT NULL",
        (model_name,)):
        meth = r["compute"]
        if not meth:
            continue
        mid = f"mt:{model_name}.{meth}"
        dep_fields = []
        dd = conn.execute(
            "SELECT depends_fields FROM decorators_detail WHERE model_name=? AND method_name=? AND decorator_type='api.depends' LIMIT 1",
            (model_name, meth),
        ).fetchone()
        if dd and dd["depends_fields"]:
            dep_fields = json.loads(dd["depends_fields"] or "[]")

        g.node(
            mid,
            f"⚡ {meth}",
            "method",
            (
                f"<b>compute: {_q(meth)}</b>"
                f"<br>Model: {_q(model_name)}"
                f"<br>For field: {_q(r['field_name'])} ({r['field_type']})"
                + (f"<br>@depends: {', '.join(dep_fields[:5])}" if dep_fields else "")
            ),
            group=model_name,
        )
        g.edge(f"m:{model_name}", mid, f"compute\n{r['field_name']}", "compute", dashes=True)

    # @api.constrains methods
    for r in _rows(conn,
        "SELECT method_name, constrains_fields FROM decorators_detail WHERE model_name=? AND decorator_type='api.constrains'",
        (model_name,)):
        meth = r["method_name"]
        fields = json.loads(r["constrains_fields"] or "[]")
        mid = f"mt:{model_name}.{meth}"
        g.node(
            mid, f"🔒 {meth}", "method",
            f"<b>@constrains</b><br>Model: {_q(model_name)}<br>Fields: {', '.join(fields[:5])}",
            group=model_name,
        )
        g.edge(f"m:{model_name}", mid, "constrains", "constrains")

    # @api.onchange methods
    for r in _rows(conn,
        "SELECT method_name, onchange_fields FROM decorators_detail WHERE model_name=? AND decorator_type='api.onchange'",
        (model_name,)):
        meth = r["method_name"]
        fields = json.loads(r["onchange_fields"] or "[]")
        mid = f"mt:{model_name}.{meth}"
        g.node(
            mid, f"🔄 {meth}", "method",
            f"<b>@onchange</b><br>Model: {_q(model_name)}<br>Fields: {', '.join(fields[:5])}",
            group=model_name,
        )
        g.edge(f"m:{model_name}", mid, "onchange", "onchange")

    # Methods with state transitions (action methods)
    for r in _rows(conn,
        "SELECT method_name, state_transitions FROM methods WHERE model_name=? AND state_transitions != '[]'",
        (model_name,)):
        meth = r["method_name"]
        trans = json.loads(r["state_transitions"] or "[]")
        mid = f"mt:{model_name}.{meth}"
        froms = {t.get("from", "?") for t in trans}
        tos = {t.get("to", "?") for t in trans}
        g.node(
            mid, f"▶ {meth}", "method",
            (
                f"<b>action: {_q(meth)}</b>"
                f"<br>Model: {_q(model_name)}"
                f"<br>Transitions: {' → '.join([f'{f}→{t}' for f,t in zip(list(froms)[:3], list(tos)[:3])])}"
            ),
            group=model_name,
        )
        g.edge(f"m:{model_name}", mid, "action", "action_meth")


def _extract_views(conn, g: GraphBuilder, model_name: str) -> None:
    """Add primary view nodes (non-inherit first, then inherit limited)."""
    rows = _rows(conn,
        "SELECT xml_id, view_type, inherit_id, module_name FROM views WHERE model=? ORDER BY inherit_id IS NOT NULL, view_type",
        (model_name,))

    for r in rows:
        xml_id = r["xml_id"] or ""
        if not xml_id:
            continue
        vtype = r["view_type"] or "unknown"
        vid = f"v:{xml_id}"
        parent_id = r["inherit_id"] or ""
        mod = r["module_name"] or ""
        icon = {"form": "📋", "list": "📄", "kanban": "🗂", "search": "🔍",
                "tree": "📄", "pivot": "📊", "graph": "📈", "calendar": "📅"}.get(vtype, "👁")
        g.node(
            vid,
            f"{icon} {xml_id.split('.')[-1] if '.' in xml_id else xml_id}\n({vtype})",
            "view",
            f"<b>{_q(xml_id)}</b><br>Type: {vtype}<br>Model: {_q(model_name)}"
            + (f"<br>Inherits: {_q(parent_id)}" if parent_id else "")
            + f"<br>Module: {_q(mod)}",
            group=mod,
        )
        g.edge(vid, f"m:{model_name}", "for model", "view_model")
        if parent_id:
            g.edge(vid, f"v:{parent_id}", "inherits", "view_inherit", dashes=True)


def _extract_states(conn, g: GraphBuilder, model_name: str) -> None:
    """Add state machine nodes and transition edges."""
    rows = _rows(conn,
        "SELECT field_name, states, transitions FROM state_machines WHERE model_name=?",
        (model_name,))
    for r in rows:
        states = json.loads(r["states"] or "[]")
        transitions = json.loads(r["transitions"] or "[]")
        field = r["field_name"]

        state_keys = []
        for s in states:
            key = s[0] if isinstance(s, (list, tuple)) else s
            label = (s[1] if isinstance(s, (list, tuple)) and len(s) > 1 else key) or key
            sid = f"st:{model_name}.{field}.{key}"
            state_keys.append((key, sid))
            g.node(sid, label, "state",
                   f"<b>State: {_q(label)}</b><br>Key: {key}<br>Model: {_q(model_name)}.{field}",
                   group=model_name)

        # model → first state
        if state_keys:
            g.edge(f"m:{model_name}", state_keys[0][1], field, "has_state", dashes=True)

        seen: set[tuple] = set()
        for t in transitions:
            frm = t.get("from") or t.get("from_state") or ""
            to = t.get("to") or t.get("to_state") or ""
            meth = t.get("method", "") or t.get("button", "") or ""
            if not to:
                continue

            # Find or create state nodes for unknown states
            frm_sid = f"st:{model_name}.{field}.{frm}" if frm else None
            to_sid = f"st:{model_name}.{field}.{to}"

            if to_sid not in g.nodes:
                g.node(to_sid, to, "state",
                       f"<b>State: {to}</b><br>Model: {_q(model_name)}.{field}")

            key = (frm, to, meth)
            if key in seen:
                continue
            seen.add(key)

            if frm_sid and frm_sid in g.nodes:
                g.edge(frm_sid, to_sid, meth or "→", "state_trans", width=2.0)
            else:
                # Transition from "any state"
                g.edge(f"m:{model_name}", to_sid, meth or "→", "state_trans")


def _extract_actions(conn, g: GraphBuilder, model_name: str) -> None:
    """Add act_window action nodes."""
    for r in _rows(conn,
        "SELECT xml_id, action_type, name, module_name FROM actions WHERE res_model=? AND action_type='act_window' LIMIT 10",
        (model_name,)):
        xml_id = r["xml_id"] or ""
        if not xml_id:
            continue
        aid = f"ac:{xml_id}"
        name = r["name"] or xml_id
        mod = r["module_name"] or ""
        g.node(aid, f"⚡ {name}", "action",
               f"<b>{_q(xml_id)}</b><br>Type: act_window<br>Model: {_q(model_name)}<br>Module: {_q(mod)}",
               group=mod)
        g.edge(aid, f"m:{model_name}", "targets", "action_tgt")


def _extract_module_graph(conn, g: GraphBuilder, module_name: str, depth: int) -> None:
    """Build a module-centric graph: module + all its models + deps."""
    visited_mods: set[str] = set()
    visited_models: set[str] = set()

    def _add_module(mod: str, d: int, is_root: bool = False) -> None:
        if mod in visited_mods:
            return
        visited_mods.add(mod)

        row = conn.execute("SELECT category, application FROM modules WHERE name=?", (mod,)).fetchone()
        cat = (row["category"] or "") if row else ""
        app = bool(row["application"]) if row else False
        g.node(
            f"mod:{mod}", mod, "module",
            f"<b>Module: {_q(mod)}</b>" + (f"<br>Category: {_q(cat)}" if cat else "") + (" · app" if app else ""),
            is_root=is_root,
        )

        # Models in this module
        models = _rows(conn, "SELECT name FROM models WHERE module_name=? AND inherit_type='primary'", (mod,))
        for mr in models:
            mn = mr["name"]
            if mn not in visited_models:
                _extract_model(conn, g, mn, 1, visited_models)

        if d <= 0:
            return

        # Dependencies
        deps = _rows(conn, "SELECT depends_on FROM module_deps WHERE module_name=?", (mod,))
        for dr in deps:
            dep = dr["depends_on"]
            g.node(f"mod:{dep}", dep, "module",
                   f"<b>Module: {_q(dep)}</b>")
            g.edge(f"mod:{mod}", f"mod:{dep}", "depends on", "module_dep")
            if d > 1:
                _add_module(dep, d - 1)

    _add_module(module_name, depth, is_root=True)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_graph_html(
    db_path: Path,
    model_name: Optional[str] = None,
    module_name: Optional[str] = None,
    depth: int = 2,
) -> tuple[str, str]:
    """Extract full knowledge graph and return (html, title).

    Pass ``model_name`` to centre on a model, or ``module_name`` for a module.
    ``depth`` controls how many hops of related models/modules to include.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Index not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        g = GraphBuilder()

        if module_name:
            _extract_module_graph(conn, g, module_name, depth)
            title = f"Module: {module_name} (depth {depth})"
        elif model_name:
            _extract_model(conn, g, model_name, depth, set(), is_root=True)
            title = f"Model: {model_name} (depth {depth})"
        else:
            raise ValueError("Provide model_name or module_name")

        data = g.to_dict(title)
        return _render_html(data), title

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HTML renderer
# ─────────────────────────────────────────────────────────────────────────────

def _render_html(data: dict) -> str:
    node_types = sorted({n.get("group", "model") for n in data["nodes"] if n.get("group")})
    # Keep only semantic types for filter (skip module-specific groups)
    _known_types = {"model", "method", "view", "state", "action", "module"}
    filter_types = [t for t in _known_types if any(n.get("group") == t for n in data["nodes"])]

    type_labels = {
        "model": ("Models", "#3b82f6"),
        "method": ("Methods", "#8b5cf6"),
        "view": ("Views", "#f97316"),
        "state": ("States", "#22c55e"),
        "action": ("Actions", "#ec4899"),
        "module": ("Modules", "#16a34a"),
    }

    edge_type_labels = {
        "inherit":     ("Inheritance", "#6366f1"),
        "field_rel":   ("Field relations", "#3b82f6"),
        "compute":     ("Compute", "#8b5cf6"),
        "constrains":  ("Constrains", "#f43f5e"),
        "onchange":    ("Onchange", "#f97316"),
        "action_meth": ("Action methods", "#ec4899"),
        "view_model":  ("View → Model", "#fb923c"),
        "view_inherit":("View inherit", "#fed7aa"),
        "state_trans": ("State transition", "#22c55e"),
        "has_state":   ("State machine", "#86efac"),
        "action_tgt":  ("Action targets", "#f472b6"),
        "module_dep":  ("Module dep", "#94a3b8"),
        "defined_in":  ("Defined in", "#64748b"),
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

    stats = (
        f"{len(data['nodes'])} nodes · {len(data['edges'])} edges"
    )

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
  header{{padding:10px 16px;background:#1e293b;border-bottom:1px solid #334155;display:flex;align-items:center;gap:12px;flex-shrink:0;min-height:48px}}
  header h1{{font-size:14px;font-weight:600;color:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:420px}}
  .badge{{font-size:10px;padding:2px 8px;border-radius:99px;background:#334155;color:#94a3b8;white-space:nowrap}}
  .stats{{font-size:11px;color:#64748b;white-space:nowrap}}
  .controls{{display:flex;gap:6px;margin-left:auto;align-items:center;flex-shrink:0}}
  .btn{{font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid #475569;background:#1e293b;color:#cbd5e1;cursor:pointer;white-space:nowrap}}
  .btn:hover{{background:#334155}}
  select.btn{{padding:3px 6px}}
  .main{{display:flex;flex:1;overflow:hidden}}
  #sidebar{{width:210px;flex-shrink:0;background:#1e293b;border-right:1px solid #334155;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:14px}}
  .sidebar-section h4{{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
  .filter-item{{display:flex;align-items:center;gap:7px;font-size:11px;color:#cbd5e1;padding:4px 8px;border-radius:4px;cursor:pointer;margin-bottom:2px}}
  .filter-item:hover{{background:#334155}}
  .filter-item input{{accent-color:#6366f1;cursor:pointer;flex-shrink:0}}
  #graph{{flex:1;position:relative}}
  #info-panel{{position:absolute;right:12px;top:12px;width:240px;background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px;font-size:12px;line-height:1.6;display:none;z-index:10;box-shadow:0 4px 24px rgba(0,0,0,.5);max-height:60vh;overflow-y:auto}}
  #info-panel h3{{font-size:13px;color:#f8fafc;margin-bottom:6px;border-bottom:1px solid #334155;padding-bottom:6px}}
  #search{{width:100%;padding:5px 8px;border-radius:5px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:11px;outline:none}}
  #search:focus{{border-color:#6366f1}}
  .search-results{{margin-top:4px;max-height:120px;overflow-y:auto}}
  .search-result-item{{font-size:10px;padding:3px 6px;border-radius:3px;cursor:pointer;color:#94a3b8}}
  .search-result-item:hover{{background:#334155;color:#e2e8f0}}
  #toggle-sidebar{{position:absolute;left:210px;top:50%;transform:translateY(-50%);z-index:20;background:#1e293b;border:1px solid #334155;border-left:none;padding:4px 3px;border-radius:0 4px 4px 0;cursor:pointer;font-size:11px;color:#64748b}}
  .legend-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}
  .legend-line{{display:inline-block;width:16px;height:2px;vertical-align:middle;margin-right:4px}}
</style>
</head>
<body>
<header>
  <h1>{data['title']}</h1>
  <span class="badge">Knowledge Graph</span>
  <span class="stats">{stats}</span>
  <div class="controls">
    <select class="btn" id="layoutSel" onchange="applyLayout(this.value)">
      <option value="force">Force</option>
      <option value="hier_ud">Hierarchical ↓</option>
      <option value="hier_lr">Hierarchical →</option>
    </select>
    <button class="btn" onclick="network.fit({{animation:true}})">Fit all</button>
    <button class="btn" id="physBtn" onclick="togglePhysics()">Pause</button>
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
      </div>
    </div>
  </div>
  <div id="graph" style="position:relative">
    <div id="toggle-sidebar" onclick="toggleSidebar()">◀</div>
    <div id="info-panel">
      <h3 id="info-title">Node Info</h3>
      <div id="info-body"></div>
    </div>
  </div>
</div>

<script>
const RAW_NODES = {json.dumps(data['nodes'], ensure_ascii=False)};
const RAW_EDGES = {json.dumps(data['edges'], ensure_ascii=False)};

const nodes = new vis.DataSet(RAW_NODES.map(n => ({{...n}})));
const edges = new vis.DataSet(RAW_EDGES.map(e => ({{...e}})));

const hiddenNodeTypes = new Set();
const hiddenEdgeTypes = new Set();

const OPTIONS = {{
  nodes:{{
    margin:{{top:6,bottom:6,left:10,right:10}},
    shadow:false,
  }},
  edges:{{
    font:{{size:9,color:'#94a3b8',background:'rgba(15,23,42,0.8)',align:'middle'}},
    smooth:{{enabled:true,type:'dynamic'}},
    selectionWidth:3,
    hoverWidth:2,
  }},
  physics:{{
    enabled:true,
    stabilization:{{iterations:300,fit:true}},
    barnesHut:{{
      gravitationalConstant:-10000,
      centralGravity:0.25,
      springLength:160,
      springConstant:0.03,
      damping:0.09,
      avoidOverlap:0.2,
    }},
  }},
  interaction:{{
    hover:true,
    tooltipDelay:200,
    zoomSpeed:0.8,
    multiselect:true,
    navigationButtons:false,
  }},
  layout:{{randomSeed:42}},
}};

const container = document.getElementById('graph');
const network = new vis.Network(container, {{nodes, edges}}, OPTIONS);

// Info panel on click
const infoPanel = document.getElementById('info-panel');
network.on('click', params => {{
  if(params.nodes.length === 0){{ infoPanel.style.display='none'; resetHighlight(); return; }}
  const nid = params.nodes[0];
  const node = nodes.get(nid);
  if(!node) return;

  // Highlight neighbourhood
  const connected = new Set(network.getConnectedNodes(nid));
  const connEdges = new Set(network.getConnectedEdges(nid));
  nodes.update(RAW_NODES.map(n => ({{
    id: n.id,
    opacity: (n.id === nid || connected.has(n.id)) ? 1 : 0.15
  }})));
  edges.update(RAW_EDGES.map(e => ({{
    id: e.id,
    opacity: connEdges.has(e.id) ? 1 : 0.06,
    width: connEdges.has(e.id) ? 2.5 : 1,
  }})));

  // Info panel
  const title = document.getElementById('info-title');
  const body = document.getElementById('info-body');
  title.textContent = node.label.replace('\\n', ' ');
  body.innerHTML = node.title || '';
  infoPanel.style.display = 'block';
}});

function resetHighlight() {{
  nodes.update(RAW_NODES.map(n => ({{id:n.id, opacity:1}})));
  edges.update(RAW_EDGES.map(e => ({{id:e.id, opacity:0.85, width: e.width || 1.5}})));
  infoPanel.style.display = 'none';
}}

// Node type filter
function toggleNodeType(cb) {{
  const t = cb.dataset.ntype;
  if(cb.checked) hiddenNodeTypes.delete(t); else hiddenNodeTypes.add(t);
  _applyFilters();
}}
// Edge type filter
function toggleEdgeType(cb) {{
  const t = cb.dataset.etype;
  if(cb.checked) hiddenEdgeTypes.delete(t); else hiddenEdgeTypes.add(t);
  _applyFilters();
}}
function _applyFilters() {{
  const hiddenNodes = new Set(RAW_NODES.filter(n => hiddenNodeTypes.has(n.group)).map(n => n.id));
  nodes.update(RAW_NODES.map(n => ({{id:n.id, hidden: hiddenNodes.has(n.id)}})));
  edges.update(RAW_EDGES.map(e => ({{
    id: e.id,
    hidden: hiddenNodes.has(e.from) || hiddenNodes.has(e.to) || hiddenEdgeTypes.has(e.edgeType||'')
  }})));
}}

// Search
function searchNodes(q) {{
  const box = document.getElementById('searchResults');
  q = q.trim().toLowerCase();
  if(!q){{ box.innerHTML=''; return; }}
  const hits = RAW_NODES.filter(n => (n.label||'').toLowerCase().includes(q) || (n.id||'').toLowerCase().includes(q)).slice(0,12);
  box.innerHTML = hits.map(n =>
    `<div class="search-result-item" onclick="focusNode('${{n.id.replace(/'/g,"\\\\'")}}')">${{n.label.replace('\\n',' ')}}</div>`
  ).join('');
}}
function focusNode(id) {{
  network.focus(id, {{scale:1.4, animation:true}});
  network.selectNodes([id]);
  document.getElementById('searchResults').innerHTML='';
  document.getElementById('search').value='';
}}

// Physics
let _physics = true;
function togglePhysics() {{
  _physics = !_physics;
  network.setOptions({{physics:{{enabled:_physics}}}});
  document.getElementById('physBtn').textContent = _physics ? 'Pause' : 'Resume';
}}

// Layout
function applyLayout(v) {{
  if(v==='force'){{
    network.setOptions({{layout:{{hierarchical:{{enabled:false}}}},physics:{{enabled:true}}}});
    _physics=true; document.getElementById('physBtn').textContent='Pause';
  }} else {{
    const dir = v==='hier_ud'?'UD':'LR';
    network.setOptions({{
      layout:{{hierarchical:{{enabled:true,direction:dir,sortMethod:'directed',levelSeparation:120,nodeSpacing:80}}}},
      physics:{{enabled:false}}
    }});
    _physics=false; document.getElementById('physBtn').textContent='Resume';
    setTimeout(()=>network.fit({{animation:true}}),300);
  }}
}}

// Sidebar
let _sidebarOpen = true;
function toggleSidebar() {{
  const sb = document.getElementById('sidebar');
  const btn = document.getElementById('toggle-sidebar');
  _sidebarOpen = !_sidebarOpen;
  sb.style.display = _sidebarOpen ? '' : 'none';
  btn.style.left = _sidebarOpen ? '210px' : '0';
  btn.textContent = _sidebarOpen ? '◀' : '▶';
  network.redraw();
}}

network.once('stabilizationIterationsDone', ()=>{{ network.fit({{animation:{{duration:600}}}}); }});
</script>
</body>
</html>"""
