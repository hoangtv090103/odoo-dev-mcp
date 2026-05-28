"""Tool 17: search_odoo_entities — FTS5 full-text search across the knowledge graph.

Finds models, fields, methods, views, and routes by name or keyword.
Uses the porter-stemmed FTS5 index for fast, fuzzy matching.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, List, Optional


async def search_odoo_entities(
    query: str,
    get_db: Callable[[], Path],
    types: Optional[List[str]] = None,
    module: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Search the Odoo knowledge graph by name or keyword.

    Uses the FTS5 search index (porter-stemmed) — supports:
      - exact substrings: ``partner``
      - prefix: ``sale*``
      - phrase: ``"action confirm"``
      - OR: ``invoice OR bill``

    Args:
        query:   Search term(s). FTS5 boolean operators supported.
        get_db:  Callable returning the SQLite db Path.
        types:   Filter by entity type(s). Valid values:
                 ``['model', 'field', 'method', 'view', 'route']``.
                 Omit (or empty list) to search all types.
        module:  Optional module name filter (exact match).
        limit:   Max results to return (default 20, max 100).

    Returns a dict with:
        query        — the original query
        results      — list of matches, each with type/name/model/module/snippet
        total        — total hit count (may exceed ``limit``)
        hint         — suggested next tool call for the top result
    """
    db_path = get_db()
    if not db_path.exists():
        return {"error": "Index not found. Run build_index() first.", "query": query}

    limit = min(max(1, limit), 100)
    valid_types = {"model", "field", "method", "view", "route"}
    type_filter = [t for t in (types or []) if t in valid_types]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # Build the FTS query — wrap raw term in quotes if it has no operators
        fts_query = _sanitize_fts_query(query)

        # Base FTS query
        base_sql = """
            SELECT entity_type, entity_name, model_context, module_name,
                   snippet(search_index, 3, '<b>', '</b>', '…', 10) AS snippet
            FROM search_index
            WHERE search_index MATCH ?
        """
        params: list = [fts_query]

        if type_filter:
            placeholders = ", ".join("?" * len(type_filter))
            base_sql += f" AND entity_type IN ({placeholders})"
            params.extend(type_filter)

        if module:
            base_sql += " AND module_name = ?"
            params.append(module)

        # Total count (without limit)
        count_sql = f"SELECT COUNT(*) FROM ({base_sql})"
        try:
            total_row = conn.execute(count_sql, params).fetchone()
            total = total_row[0] if total_row else 0
        except sqlite3.OperationalError:
            total = 0

        # Fetch results
        fetch_sql = base_sql + f" ORDER BY rank LIMIT {limit}"
        try:
            rows = conn.execute(fetch_sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            return {
                "error": f"FTS query error: {exc}",
                "query": query,
                "hint": "Try simpler terms. FTS5 operators: AND, OR, NOT, *, \"phrase\".",
            }

        results = []
        for r in rows:
            entry: dict = {
                "type": r["entity_type"],
                "name": r["entity_name"],
                "module": r["module_name"],
                "snippet": r["snippet"] or "",
            }
            ctx = r["model_context"]
            if ctx:
                entry["model"] = ctx
            results.append(entry)

        # Build a hint for the top result
        hint = _build_hint(results[0]) if results else None

        return {
            "query": query,
            "results": results,
            "total": total,
            "returned": len(results),
            **({"hint": hint} if hint else {}),
        }

    finally:
        conn.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

_FTS_OPERATORS = {"AND", "OR", "NOT"}
_FTS_SPECIALS = {"*", '"', "(", ")"}


def _sanitize_fts_query(raw: str) -> str:
    """Wrap a plain search term in double-quotes so special chars don't break FTS5.

    If the query already looks like it contains FTS operators/wildcards, pass
    it through as-is so advanced users can use boolean syntax.
    """
    stripped = raw.strip()
    # If the user typed operators or quotes, leave as-is
    words = stripped.split()
    has_operators = any(w.upper() in _FTS_OPERATORS for w in words)
    has_specials = any(ch in stripped for ch in _FTS_SPECIALS)
    if has_operators or has_specials:
        return stripped
    # Simple term: auto-add prefix wildcard for partial matching
    return f'"{stripped}"*' if " " not in stripped else f'"{stripped}"'


def _build_hint(hit: dict) -> str:
    """Suggest the most useful next tool call for the top search result."""
    t = hit.get("type")
    name = hit.get("name", "")
    model = hit.get("model") or name

    if t == "model":
        return f'get_model_schema("{name}", compact=True)  — quick field overview'
    if t == "field":
        return f'trace_compute_chain("{model}", "{name}")  — or  get_field_visibility("{model}", "{name}")'
    if t == "method":
        return f'get_method_logic("{model}", "{name}")  — decorators, transitions, ORM calls'
    if t == "view":
        return f'resolve_xml_view("{model}")  — merged view XML'
    if t == "route":
        return f'get_http_routes(path_prefix="{name[:30]}")  — route details'
    return f'get_project_context(focus_model="{model}")'
