"""Tool 18: trace_odoo_path — multi-hop BFS graph walk through the knowledge graph.

Starts from a model and follows ALL relationship types (field relations,
_inherit chains, compute dependencies, state transitions, actions) up to
`depth` hops, collecting a compact node+edge map.

Stops early when the token budget is consumed so responses stay bounded.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional


def _parse_inherit(raw: str | None) -> list[str]:
    """Normalise inherit_model column — plain string or JSON array."""
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


# Approximate tokens per node/edge entry (conservative)
_TOKENS_PER_NODE = 15
_TOKENS_PER_EDGE = 10


async def trace_odoo_path(
    start_model: str,
    get_db: Callable[[], Path],
    depth: int = 2,
    edge_types: Optional[List[str]] = None,
    token_budget: int = 3000,
) -> dict:
    """Walk the Odoo knowledge graph from a model, following all relationship edges.

    Performs a breadth-first search up to ``depth`` hops.  Stops adding new
    nodes once the estimated token budget is exhausted so the response always
    fits in the AI's context window.

    Edge types traversed (all enabled by default):
      - ``field_rel``   — Many2one / One2many / Many2many field links
      - ``inherit``     — _inherit / _inherits chain (up and down)
      - ``compute``     — field compute-method dependency
      - ``state``       — state machine transitions
      - ``action``      — act_window / server action references

    Args:
        start_model:   Starting Odoo model (e.g. ``'sale.order'``).
        get_db:        Callable returning the SQLite db Path.
        depth:         Max hops to follow (1–4, default 2).
        edge_types:    Subset of edge types to follow.  Omit for all types.
        token_budget:  Approximate token limit for the response.  Nodes are
                       added until the budget is reached.  Default 3000.

    Returns a dict with:
        start        — the root model name
        depth        — actual depth explored
        nodes        — list of {id, type, label, module}
        edges        — list of {from, to, type, label}
        truncated    — True if budget stopped early expansion
        hint         — suggested tool call for the densest connected node
    """
    db_path = get_db()
    if not db_path.exists():
        return {"error": "Index not found. Run build_index() first.", "start": start_model}

    depth = max(1, min(depth, 4))
    valid_edge_types = {"field_rel", "inherit", "compute", "state", "action"}
    active_edges = (
        {t for t in edge_types if t in valid_edge_types}
        if edge_types
        else valid_edge_types
    )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # Check start model exists
        start_row = conn.execute(
            "SELECT name, module_name, description FROM models WHERE name = ? LIMIT 1",
            (start_model,),
        ).fetchone()
        if not start_row:
            # Try FTS fallback suggestion
            like = conn.execute(
                "SELECT DISTINCT name FROM models WHERE name LIKE ? LIMIT 5",
                (f"%{start_model}%",),
            ).fetchall()
            return {
                "error": f"Model '{start_model}' not found in index.",
                "suggestions": [r["name"] for r in like],
                "hint": f'search_odoo_entities("{start_model}")  — find correct model name',
            }

        nodes: dict[str, dict] = {}  # node_id → node dict
        edges: list[dict] = []
        edge_keys: set[tuple] = set()
        truncated = False
        tokens_used = 0

        def _add_node(node_id: str, node_type: str, label: str, module: str = "") -> bool:
            nonlocal tokens_used
            if node_id in nodes:
                return False
            if tokens_used + _TOKENS_PER_NODE > token_budget:
                return False
            nodes[node_id] = {"id": node_id, "type": node_type, "label": label, "module": module}
            tokens_used += _TOKENS_PER_NODE
            return True

        def _add_edge(from_id: str, to_id: str, edge_type: str, label: str = "") -> None:
            nonlocal tokens_used
            key = (from_id, to_id, edge_type)
            if key in edge_keys:
                return
            if tokens_used + _TOKENS_PER_EDGE > token_budget:
                return
            edge_keys.add(key)
            edges.append({"from": from_id, "to": to_id, "type": edge_type, "label": label})
            tokens_used += _TOKENS_PER_EDGE

        # ── Root node ─────────────────────────────────────────────────────────
        _add_node(
            f"m:{start_model}", "model", start_model,
            start_row["module_name"] or "",
        )

        # BFS queue: (model_name, current_depth)
        queue: deque[tuple[str, int]] = deque([(start_model, 0)])
        visited_models: set[str] = {start_model}

        while queue:
            model, hop = queue.popleft()
            if hop >= depth:
                continue

            model_node = f"m:{model}"

            # ── field_rel edges ───────────────────────────────────────────────
            if "field_rel" in active_edges:
                rows = conn.execute(
                    """
                    SELECT field_name, field_type, comodel_name, module_name
                    FROM fields
                    WHERE model_name = ?
                      AND field_type IN ('Many2one','One2many','Many2many')
                      AND comodel_name IS NOT NULL
                    """,
                    (model,),
                ).fetchall()
                for r in rows:
                    comodel = r["comodel_name"]
                    target_node = f"m:{comodel}"
                    if tokens_used + _TOKENS_PER_NODE + _TOKENS_PER_EDGE > token_budget:
                        truncated = True
                        break
                    added = _add_node(target_node, "model", comodel, r["module_name"] or "")
                    _add_edge(model_node, target_node, "field_rel", f"{r['field_name']}:{r['field_type']}")
                    if added and comodel not in visited_models:
                        visited_models.add(comodel)
                        queue.append((comodel, hop + 1))

            # ── inherit edges (upstream _inherit) ─────────────────────────────
            if "inherit" in active_edges:
                rows = conn.execute(
                    "SELECT inherit_model FROM models WHERE name = ? AND inherit_model IS NOT NULL",
                    (model,),
                ).fetchall()
                for r in rows:
                    for parent in _parse_inherit(r["inherit_model"]):
                        target_node = f"m:{parent}"
                        _add_node(target_node, "model", parent)
                        _add_edge(model_node, target_node, "inherit", "_inherit")
                        if parent not in visited_models:
                            visited_models.add(parent)
                            queue.append((parent, hop + 1))

                # downstream _inherit (children)
                children = conn.execute(
                    "SELECT DISTINCT name, module_name FROM models WHERE inherit_model = ? AND inherit_type = '_inherit'",
                    (model,),
                ).fetchall()
                for r in children:
                    child = r["name"]
                    if tokens_used + _TOKENS_PER_NODE + _TOKENS_PER_EDGE > token_budget:
                        truncated = True
                        break
                    child_node = f"m:{child}"
                    _add_node(child_node, "model", child, r["module_name"] or "")
                    _add_edge(child_node, model_node, "inherit", "_inherit↓")

            # ── compute edges ─────────────────────────────────────────────────
            if "compute" in active_edges:
                rows = conn.execute(
                    """
                    SELECT f.field_name, f.compute
                    FROM fields f
                    WHERE f.model_name = ? AND f.compute IS NOT NULL
                    """,
                    (model,),
                ).fetchall()
                for r in rows:
                    method_node = f"mt:{model}.{r['compute']}"
                    if tokens_used + _TOKENS_PER_NODE > token_budget:
                        truncated = True
                        break
                    _add_node(method_node, "method", f"{r['compute']}()", "")
                    _add_edge(method_node, f"f:{model}.{r['field_name']}", "compute", "computes")
                    # Also add the field as a node (lightweight)
                    field_node = f"f:{model}.{r['field_name']}"
                    _add_node(field_node, "field", f"{model}.{r['field_name']}")

            # ── state edges ───────────────────────────────────────────────────
            if "state" in active_edges:
                rows = conn.execute(
                    """
                    SELECT field_name, state_key, transitions
                    FROM state_machines
                    WHERE model_name = ?
                    """,
                    (model,),
                ).fetchall()
                for r in rows:
                    import json
                    try:
                        transitions = json.loads(r["transitions"] or "[]")
                    except (ValueError, TypeError):
                        continue
                    for t in transitions:
                        src = t.get("from")
                        dst = t.get("to")
                        if not src or not dst:
                            continue
                        src_node = f"st:{model}.{r['field_name']}.{src}"
                        dst_node = f"st:{model}.{r['field_name']}.{dst}"
                        _add_node(src_node, "state", f"{src}", "")
                        _add_node(dst_node, "state", f"{dst}", "")
                        trigger = t.get("trigger") or t.get("method") or ""
                        _add_edge(src_node, dst_node, "state", trigger)

            # ── action edges ──────────────────────────────────────────────────
            if "action" in active_edges:
                rows = conn.execute(
                    "SELECT xml_id, name FROM actions WHERE res_model = ? LIMIT 10",
                    (model,),
                ).fetchall()
                for r in rows:
                    action_node = f"ac:{r['xml_id'] or r['name']}"
                    if tokens_used + _TOKENS_PER_NODE + _TOKENS_PER_EDGE > token_budget:
                        truncated = True
                        break
                    _add_node(action_node, "action", r["name"] or r["xml_id"], "")
                    _add_edge(action_node, model_node, "action", "opens")

        # ── Build hint ────────────────────────────────────────────────────────
        # Suggest the most-connected non-root model node
        model_nodes = [n for n in nodes.values() if n["type"] == "model" and n["id"] != f"m:{start_model}"]
        degree: dict[str, int] = {}
        for e in edges:
            degree[e["from"]] = degree.get(e["from"], 0) + 1
            degree[e["to"]]   = degree.get(e["to"],   0) + 1

        hint = None
        if model_nodes:
            top = max(model_nodes, key=lambda n: degree.get(n["id"], 0))
            hint = f'get_model_schema("{top["label"]}", compact=True)  — {degree.get(top["id"], 0)} connections'

        return {
            "start": start_model,
            "depth": depth,
            "nodes": list(nodes.values()),
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "truncated": truncated,
            **({"hint": hint} if hint else {}),
        }

    finally:
        conn.close()
