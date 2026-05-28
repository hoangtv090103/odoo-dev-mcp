"""
Incremental indexing helpers.

Tracks a per-module hash of all source files so the pipeline can skip
modules that have not changed since the last index run.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import xxhash as _xxhash
    def _hash_bytes(data: bytes) -> str:
        return _xxhash.xxh3_64(data).hexdigest()
except ImportError:
    import hashlib
    def _hash_bytes(data: bytes) -> str:  # type: ignore[misc]
        return hashlib.sha256(data).hexdigest()[:16]

if TYPE_CHECKING:
    from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── Hash computation ──────────────────────────────────────────────────────────

def compute_module_hash(module: "ModuleRecord") -> str:
    """Return a stable hash of all source files inside a module.

    The hash covers every .py, .xml, .csv, and .js file tracked by the
    ModuleRecord.  File paths are included in the hash so renames are
    also detected.
    """
    all_files = sorted(
        module.python_files + module.xml_files + module.csv_files + module.js_files,
        key=str,
    )
    parts: list[str] = []
    for f in all_files:
        try:
            content = f.read_bytes()
            parts.append(f"{f}:{_hash_bytes(content)}")
        except (IOError, OSError) as exc:
            logger.debug("Cannot read %s for hashing: %s", f, exc)

    combined = "\n".join(parts).encode()
    return _hash_bytes(combined)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_stored_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {module_name: hash} for all rows in file_hashes."""
    try:
        rows = conn.execute("SELECT module_name, hash FROM file_hashes").fetchall()
        return {r[0]: r[1] for r in rows}
    except sqlite3.OperationalError:
        return {}


def store_module_hashes(
    conn: sqlite3.Connection,
    modules: "list[ModuleRecord]",
    hashes: dict[str, str],
) -> None:
    """Upsert hash rows for the given modules."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO file_hashes (module_name, hash, indexed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(module_name) DO UPDATE SET
            hash       = excluded.hash,
            indexed_at = excluded.indexed_at
        """,
        [
            (m.name, hashes[m.name], now)
            for m in modules
            if m.name in hashes
        ],
    )
    conn.commit()


# ── Change detection ──────────────────────────────────────────────────────────

def find_changed_modules(
    modules: "list[ModuleRecord]",
    stored_hashes: dict[str, str],
) -> tuple["list[ModuleRecord]", "list[ModuleRecord]"]:
    """Split modules into (changed, unchanged) based on stored hashes.

    A module is considered *changed* if:
    - it has no stored hash (new module), or
    - its current hash differs from the stored hash.

    Returns:
        (changed_modules, unchanged_modules)
    """
    changed: list[ModuleRecord] = []
    unchanged: list[ModuleRecord] = []

    for module in modules:
        current_hash = compute_module_hash(module)
        stored = stored_hashes.get(module.name)
        if stored is None or stored != current_hash:
            changed.append(module)
        else:
            unchanged.append(module)

    return changed, unchanged


# ── Cleanup ───────────────────────────────────────────────────────────────────

# Tables that have a module_name column and can be cleaned per-module
_MODULE_TABLES = [
    "models",
    "fields",
    "methods",
    "views",
    "decorators_detail",
    "http_routes",
    "actions",
    "menus",
    "cron_jobs",
    "qweb_templates",
    "email_templates",
    "view_element_refs",
    "field_groups_map",
    "selection_extensions",
    "context_dependencies",
    "js_components",
    "access_rules",
    "record_rules",
    "related_field_paths",
    "state_machines",
]


def delete_module_data(conn: sqlite3.Connection, module_name: str) -> None:
    """Delete all indexed data for a single module before re-indexing it."""
    for table in _MODULE_TABLES:
        try:
            conn.execute(f"DELETE FROM {table} WHERE module_name = ?", (module_name,))
        except sqlite3.OperationalError as exc:
            logger.debug("Cannot clean %s for module %s: %s", table, module_name, exc)

    # FTS5 search_index — delete by module_name
    try:
        conn.execute("DELETE FROM search_index WHERE module_name = ?", (module_name,))
    except sqlite3.OperationalError:
        pass

    conn.commit()
    logger.debug("Cleared data for module '%s'", module_name)
