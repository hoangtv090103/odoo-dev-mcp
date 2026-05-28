"""Tool 15: Visualize Odoo models / modules as Mermaid diagrams or JSON graphs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable, Optional


# ── Public entry point ────────────────────────────────────────────────────────

async def get_model_graph(
    model_name: str,
    graph_type: str = "relations",
    depth: int = 1,
    output_format: str = "mermaid",
    get_db: Callable[[], Path] = None,
) -> dict:
    """Generate a visual graph of an Odoo model or module.

    Args:
        model_name:    Odoo model (e.g. 'sale.order') — or module name for
                       graph_type='module_deps'.
        graph_type:    One of:
                         'relations'    — field-level Many2one/One2many/Many2many
                         'state_machine'— state + transition diagram
                         'inheritance'  — _inherit / _inherits chain
                         'module_deps'  — module dependency tree
        depth:         Hop depth (1–3). Only used for 'relations' and 'module_deps'.
        output_format: 'mermaid' (ready-to-render) or 'json' (raw nodes+edges).
        get_db:        Callable returning the SQLite db Path.

    Returns:
        dict with 'diagram' (mermaid string) or 'data' (json), plus metadata.
    """
    db_path = get_db()
    if not db_path.exists():
        return {"error": "Index not found. Run build_index first."}

    depth = max(1, min(int(depth), 3))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        if graph_type == "relations":
            return _graph_relations(conn, model_name, depth, output_format)
        elif graph_type == "state_machine":
            return _graph_state_machine(conn, model_name, output_format)
        elif graph_type == "inheritance":
            return _graph_inheritance(conn, model_name, output_format)
        elif graph_type == "module_deps":
            return _graph_module_deps(conn, model_name, depth, output_format)
        else:
            return {
                "error": f"Unknown graph_type '{graph_type}'.",
                "valid_types": ["relations", "state_machine", "inheritance", "module_deps"],
            }
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sid(name: str) -> str:
    """Safe Mermaid node ID — replace dots/spaces/hyphens with underscores."""
    return name.replace(".", "_").replace("-", "_").replace(" ", "_").replace("/", "_")


def _q(s: str) -> str:
    """Quote a string for Mermaid labels (escape double quotes)."""
    return s.replace('"', "'")


def _rows(conn, sql: str, params=()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


# ── Graph 1: Relations ────────────────────────────────────────────────────────

_REL_TYPES = ("Many2one", "One2many", "Many2many", "Many2oneReference")


def _collect_relations(conn, model_name: str, depth: int) -> tuple[set, list]:
    """BFS over relational fields up to `depth` hops."""
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(model_name, 0)]
    edges: list[dict] = []

    while queue:
        current, d = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        rows = _rows(
            conn,
            """
            SELECT field_name, field_type, comodel_name, string_label
            FROM fields
            WHERE model_name = ?
              AND field_type IN ('Many2one','One2many','Many2many','Many2oneReference')
              AND comodel_name IS NOT NULL
              AND comodel_name != ''
            ORDER BY field_type, field_name
            """,
            (current,),
        )
        for r in rows:
            target = r["comodel_name"]
            edges.append(
                {
                    "from": current,
                    "to": target,
                    "field_name": r["field_name"],
                    "field_type": r["field_type"],
                    "label": r["string_label"] or r["field_name"],
                }
            )
            if d < depth - 1 and target not in visited:
                queue.append((target, d + 1))

    return visited, edges


def _graph_relations(conn, model_name: str, depth: int, fmt: str) -> dict:
    nodes_set, edges = _collect_relations(conn, model_name, depth)

    if not nodes_set:
        return {"error": f"Model '{model_name}' not found in index."}

    nodes = [{"model": m} for m in sorted(nodes_set)]

    if fmt == "json":
        return {
            "graph_type": "relations",
            "model": model_name,
            "depth": depth,
            "format": "json",
            "nodes": nodes,
            "edges": edges,
        }

    # ── Mermaid erDiagram ──────────────────────────────────────────────────
    lines = ["erDiagram"]

    # Gather key fields per model for entity body
    for node in nodes:
        m = node["model"]
        mid = _sid(m)
        field_rows = _rows(
            conn,
            """
            SELECT field_name, field_type, string_label, required, compute
            FROM fields
            WHERE model_name = ?
              AND field_type NOT IN ('One2many','Many2many')
            ORDER BY
              CASE WHEN field_type = 'Many2one' THEN 0 ELSE 1 END,
              field_name
            LIMIT 12
            """,
            (m,),
        )
        lines.append(f"    {mid} {{")
        for f in field_rows:
            ft = f["field_type"].replace("2", "2")  # keep as-is
            fn = f["field_name"]
            label = f["string_label"] or ""
            suffix = " PK" if fn == "id" else (" FK" if f["field_type"] == "Many2one" else "")
            comment = f' "{_q(label)}"' if label and label.lower() != fn.lower() else ""
            lines.append(f"        {ft} {fn}{suffix}{comment}")
        lines.append("    }")

    lines.append("")

    # Relationships
    for e in edges:
        src = _sid(e["from"])
        tgt = _sid(e["to"])
        fn = e["field_name"]
        ft = e["field_type"]

        if ft == "Many2one":
            rel = f"}}o--||"
        elif ft == "One2many":
            rel = f"||--o{{"
        elif ft == "Many2many":
            rel = f"}}o--o{{"
        else:
            rel = f"}}|--|{{"

        lines.append(f'    {src} {rel} {tgt} : "{_q(fn)}"')

    diagram = "\n".join(lines)
    return {
        "graph_type": "relations",
        "model": model_name,
        "depth": depth,
        "format": "mermaid",
        "diagram": diagram,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "usage_hint": "Render this Mermaid diagram to see model relationships.",
    }


# ── Graph 2: State Machine ────────────────────────────────────────────────────

def _graph_state_machine(conn, model_name: str, fmt: str) -> dict:
    row = conn.execute(
        "SELECT states, transitions, field_name FROM state_machines WHERE model_name = ?",
        (model_name,),
    ).fetchone()

    if not row:
        # Try partial match
        row = conn.execute(
            "SELECT states, transitions, field_name, model_name "
            "FROM state_machines WHERE model_name LIKE ?",
            (f"%{model_name}%",),
        ).fetchone()
        if not row:
            return {"error": f"No state machine found for model '{model_name}'."}

    states = json.loads(row["states"] or "[]")
    transitions = json.loads(row["transitions"] or "[]")
    field_name = row["field_name"]

    if fmt == "json":
        return {
            "graph_type": "state_machine",
            "model": model_name,
            "field": field_name,
            "format": "json",
            "states": states,
            "transitions": transitions,
        }

    # ── Mermaid stateDiagram-v2 ────────────────────────────────────────────
    lines = ["stateDiagram-v2"]

    # State definitions with labels
    for s in states:
        key = s[0] if isinstance(s, (list, tuple)) else s
        label = (s[1] if isinstance(s, (list, tuple)) and len(s) > 1 else key) or key
        if key and label and _q(label) != key:
            lines.append(f'    state "{_q(label)}" as {_sid(key)}')

    lines.append("")
    lines.append("    [*] --> " + (_sid(states[0][0]) if states and isinstance(states[0], (list, tuple)) else _sid(states[0]) if states else "draft"))

    seen_transitions: set[tuple] = set()
    for t in transitions:
        frm = t.get("from") or t.get("from_state", "")
        to = t.get("to") or t.get("to_state", "")
        method = t.get("method", "") or t.get("button", "") or ""
        if not frm or not to:
            continue
        key = (frm, to, method)
        if key in seen_transitions:
            continue
        seen_transitions.add(key)
        label = f" : {_q(method)}" if method else ""
        lines.append(f"    {_sid(frm)} --> {_sid(to)}{label}")

    diagram = "\n".join(lines)
    return {
        "graph_type": "state_machine",
        "model": model_name,
        "field": field_name,
        "format": "mermaid",
        "diagram": diagram,
        "state_count": len(states),
        "transition_count": len(transitions),
    }


# ── Graph 3: Inheritance ──────────────────────────────────────────────────────

def _graph_inheritance(conn, model_name: str, fmt: str) -> dict:
    """Build full _inherit/_inherits chain for a model (up + down)."""
    # Get all models that inherit from this model (downstream)
    downstream = _rows(
        conn,
        """
        SELECT name, inherit_model, inherit_type, module_name
        FROM models
        WHERE inherit_model = ? AND inherit_type = '_inherit'
        """,
        (model_name,),
    )
    # Get models this model inherits from (upstream)
    upstream = _rows(
        conn,
        """
        SELECT name, inherit_model, inherit_type, module_name
        FROM models
        WHERE name = ? AND inherit_model IS NOT NULL
        """,
        (model_name,),
    )
    # Get all definitions (primary + _inherit in same model)
    all_defs = _rows(
        conn,
        """
        SELECT name, inherit_model, inherit_type, module_name, python_class
        FROM models
        WHERE name = ?
        ORDER BY inherit_type
        """,
        (model_name,),
    )

    edges = []
    nodes: dict[str, dict] = {}

    def _add_node(m: str, mod: str = "", cls: str = "") -> None:
        if m not in nodes:
            nodes[m] = {"model": m, "module": mod, "class": cls}

    for r in all_defs:
        _add_node(r["name"], r["module_name"] or "", r["python_class"] or "")

    for r in upstream:
        parent = r["inherit_model"]
        child = r["name"]
        _add_node(child, r["module_name"] or "")
        _add_node(parent)
        edges.append({"from": child, "to": parent, "type": r["inherit_type"]})

    for r in downstream:
        child = r["name"]
        parent = r["inherit_model"]
        _add_node(child, r["module_name"] or "")
        _add_node(parent)
        edges.append({"from": child, "to": parent, "type": r["inherit_type"]})

    if fmt == "json":
        return {
            "graph_type": "inheritance",
            "model": model_name,
            "format": "json",
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    # ── Mermaid graph TD ──────────────────────────────────────────────────
    lines = ["graph TD"]

    for n in nodes.values():
        m = n["model"]
        mod = n["module"]
        mid = _sid(m)
        label = f"{_q(m)}"
        if mod:
            label += f"\\n[{_q(mod)}]"
        style = ' style ' + mid + ' fill:#e8f4fd,stroke:#6366f1,color:#1e1b4b' if m == model_name else ''
        lines.append(f'    {mid}["{label}"]{style}')

    lines.append("")
    for e in edges:
        src = _sid(e["from"])
        tgt = _sid(e["to"])
        itype = e["type"]
        if itype == "_inherit":
            lines.append(f"    {src} -->|_inherit| {tgt}")
        elif itype == "_inherits":
            lines.append(f"    {src} -.->|_inherits| {tgt}")
        else:
            lines.append(f"    {src} --> {tgt}")

    diagram = "\n".join(lines)
    return {
        "graph_type": "inheritance",
        "model": model_name,
        "format": "mermaid",
        "diagram": diagram,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ── Graph 4: Module Dependencies ──────────────────────────────────────────────

def _collect_module_deps(conn, module_name: str, depth: int) -> tuple[set, list]:
    """BFS over module_deps table up to `depth` hops."""
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(module_name, 0)]
    edges: list[dict] = []

    while queue:
        current, d = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if d >= depth:
            continue

        deps = _rows(
            conn,
            "SELECT depends_on FROM module_deps WHERE module_name = ?",
            (current,),
        )
        for r in deps:
            dep = r["depends_on"]
            edges.append({"from": current, "to": dep})
            if dep not in visited:
                queue.append((dep, d + 1))

    return visited, edges


def _graph_module_deps(conn, module_name: str, depth: int, fmt: str) -> dict:
    # Verify module exists
    exists = conn.execute(
        "SELECT 1 FROM modules WHERE name = ? LIMIT 1", (module_name,)
    ).fetchone()
    if not exists:
        # Try partial match
        like = conn.execute(
            "SELECT name FROM modules WHERE name LIKE ? LIMIT 5", (f"%{module_name}%",)
        ).fetchall()
        names = [r["name"] for r in like]
        return {
            "error": f"Module '{module_name}' not found.",
            "suggestions": names,
        }

    nodes_set, edges = _collect_module_deps(conn, module_name, depth)

    # Enrich nodes with module metadata
    nodes = []
    for m in sorted(nodes_set):
        row = conn.execute(
            "SELECT version, category, application FROM modules WHERE name = ?", (m,)
        ).fetchone()
        nodes.append(
            {
                "module": m,
                "version": row["version"] if row else "",
                "category": row["category"] if row else "",
                "application": bool(row["application"]) if row else False,
                "is_root": m == module_name,
            }
        )

    if fmt == "json":
        return {
            "graph_type": "module_deps",
            "module": module_name,
            "depth": depth,
            "format": "json",
            "nodes": nodes,
            "edges": edges,
        }

    # ── Mermaid graph TD ──────────────────────────────────────────────────
    lines = ["graph TD"]

    for n in nodes:
        m = n["module"]
        mid = _sid(m)
        cat = n["category"] or ""
        label = f"{_q(m)}"
        if cat:
            label += f"\\n{_q(cat)}"
        style = ""
        if n["is_root"]:
            style = f"\n    style {mid} fill:#e8f4fd,stroke:#6366f1,color:#1e1b4b,font-weight:bold"
        elif n["application"]:
            style = f"\n    style {mid} fill:#fef3c7,stroke:#f59e0b"
        lines.append(f'    {mid}["{label}"]{style}')

    lines.append("")
    for e in edges:
        src = _sid(e["from"])
        tgt = _sid(e["to"])
        lines.append(f"    {src} --> {tgt}")

    diagram = "\n".join(lines)
    return {
        "graph_type": "module_deps",
        "module": module_name,
        "depth": depth,
        "format": "mermaid",
        "diagram": diagram,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }
