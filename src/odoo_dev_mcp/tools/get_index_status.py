"""Tool 14: get_index_status — check index health and staleness."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, async_query_one


def _compute_stale(conn, config) -> int:
    """Return the number of stale + new modules by comparing file hashes.

    Uses an already-open ``conn`` (read-only is fine).  Called by both
    ``get_index_status`` and ``get_project_context`` to avoid duplicate work.
    """
    from ..indexer.hashing import compute_module_hash
    from ..indexer.module_scanner import scan_all_paths

    try:
        stored = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT module_name, hash FROM file_hashes"
            ).fetchall()
        }
        all_modules = scan_all_paths(config)
        count = 0
        for m in all_modules:
            current = compute_module_hash(m)
            if m.name not in stored or stored[m.name] != current:
                count += 1
        return count
    except Exception:
        return 0


async def get_index_status(
    get_db: Callable[[], Path],
    get_config,
) -> dict:
    """Get the current status of the Odoo knowledge graph index.

    Call this tool at the start of a session or before using other tools
    to check whether the index exists and is up to date.

    Returns information about:
    - Whether an index exists
    - When it was last built
    - How many modules, models, and fields are indexed
    - How many modules have changed on disk since last index (stale count)
    """
    import sqlite3

    config = get_config()
    db_path = config.db_path

    if not db_path.exists():
        return {
            "has_index": False,
            "message": "No index found. Call build_index to create one.",
            "addons_paths": [str(p) for p in config.all_paths],
        }

    # Read metadata from DB
    meta: dict = {}
    counts: dict = {}
    try:
        rows = await async_query(db_path, "SELECT key, value FROM index_meta", ())
        meta = {r["key"]: r["value"] for r in rows}

        for table, label in [
            ("modules", "modules"),
            ("models", "models"),
            ("fields", "fields"),
            ("methods", "methods"),
            ("views", "views"),
            ("http_routes", "routes"),
        ]:
            row = await async_query_one(db_path, f"SELECT COUNT(*) as n FROM {table}", ())
            counts[label] = row["n"] if row else 0
    except Exception as exc:
        return {
            "has_index": True,
            "error": f"Could not read index metadata: {exc}",
            "db_path": str(db_path),
        }

    # Count stale + new modules — detailed breakdown for tool response
    stale_modules: list[str] = []
    new_modules: list[str] = []
    try:
        from ..indexer.hashing import compute_module_hash
        from ..indexer.module_scanner import scan_all_paths
        all_modules = scan_all_paths(config)
        conn_ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn_ro.row_factory = sqlite3.Row
        stored = {r["module_name"]: r["hash"]
                  for r in conn_ro.execute("SELECT module_name, hash FROM file_hashes").fetchall()}
        conn_ro.close()
        for m in all_modules:
            current = compute_module_hash(m)
            if m.name not in stored:
                new_modules.append(m.name)
            elif stored[m.name] != current:
                stale_modules.append(m.name)
    except Exception:
        pass  # staleness check is best-effort

    stale_count = len(stale_modules) + len(new_modules)

    status = {
        "has_index": True,
        "last_indexed": meta.get("indexed_at", "unknown"),
        "odoo_version": meta.get("odoo_version_hint") or "unknown",
        "schema_version": meta.get("schema_version", "unknown"),
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "counts": counts,
        "stale_modules": stale_count,
        "stale_module_names": stale_modules[:10],   # cap to avoid huge response
        "new_module_names": new_modules[:10],
        "addons_paths": [str(p) for p in config.all_paths],
    }

    if stale_count == 0:
        status["message"] = "Index is up to date."
    else:
        status["message"] = (
            f"Index has {stale_count} stale/new module(s). "
            "Consider calling build_index() to update."
        )

    return status
