"""Tool 12: get_http_routes — HTTP/JSON-RPC routes exposed by Odoo modules."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..db.connection import async_query, json_col


async def get_http_routes(
    get_db: Callable[[], Path],
    module_name: Optional[str] = None,
    path_prefix: Optional[str] = None,
    auth_filter: Optional[str] = None,
) -> dict:
    """List HTTP/JSON-RPC routes exposed by Odoo modules, with auth requirements
    and path parameters."""
    db_path = get_db()

    # Build dynamic WHERE clause
    conditions = []
    params: list = []

    if module_name:
        conditions.append("module_name = ?")
        params.append(module_name)

    if path_prefix:
        conditions.append("route_pattern LIKE ?")
        params.append(f"{path_prefix}%")

    if auth_filter:
        conditions.append("auth = ?")
        params.append(auth_filter)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    route_rows = await async_query(
        db_path,
        f"""
        SELECT route_pattern, route_patterns, auth, route_type,
               http_methods, website, sitemap, cors, csrf,
               controller_class, method_name, module_name,
               file_path, line_number, path_params
        FROM http_routes
        {where_clause}
        ORDER BY module_name, route_pattern
        """,
        tuple(params),
    )

    routes = []
    for row in route_rows:
        # route_patterns is JSON list; fall back to route_pattern string
        patterns = json_col(row, "route_patterns", None)
        if patterns is None:
            patterns = [row.get("route_pattern")]

        routes.append(
            {
                "patterns": patterns,
                "primary_pattern": row.get("route_pattern"),
                "auth": row.get("auth"),
                "type": row.get("route_type"),
                "methods": json_col(row, "http_methods", ["GET", "POST"]),
                "website": bool(row.get("website", 0)),
                "sitemap": bool(row.get("sitemap", 0)),
                "cors": row.get("cors"),
                "csrf": bool(row.get("csrf", 1)),
                "controller_class": row.get("controller_class"),
                "method": row.get("method_name"),
                "module": row.get("module_name"),
                "file": row.get("file_path"),
                "line": row.get("line_number"),
                "path_params": json_col(row, "path_params", []),
            }
        )

    # Aggregate by auth type for summary
    auth_counts: dict[str, int] = {}
    for r in routes:
        a = r["auth"] or "none"
        auth_counts[a] = auth_counts.get(a, 0) + 1

    # Aggregate by module
    module_counts: dict[str, int] = {}
    for r in routes:
        m = r["module"] or "unknown"
        module_counts[m] = module_counts.get(m, 0) + 1

    return {
        "routes": routes,
        "total": len(routes),
        "filters_applied": {
            "module_name": module_name,
            "path_prefix": path_prefix,
            "auth_filter": auth_filter,
        },
        "summary": {
            "by_auth": auth_counts,
            "by_module": module_counts,
        },
    }
