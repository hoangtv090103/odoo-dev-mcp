"""
SQLite connection management for Odoo Dev MCP.

Provides both sync (for indexer) and async (for MCP tools) access.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from .schema import SCHEMA_SQL, DROP_ALL_SQL


# ── Sync connection (used by indexer) ─────────────────────────────────────────

def open_db(db_path: Path, *, reset: bool = False) -> sqlite3.Connection:
    """Open (or create) the SQLite database at db_path.

    Args:
        db_path: Path to the .db file.
        reset:   If True, drop all tables and recreate the schema.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if reset:
        conn.executescript(DROP_ALL_SQL)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@contextmanager
def get_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a read-only(-ish) connection for query tools."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── Async connection (used by MCP tools) ──────────────────────────────────────

try:
    import aiosqlite

    async def async_query(
        db_path: Path,
        sql: str,
        params: tuple = (),
    ) -> list[dict]:
        """Execute a SELECT query asynchronously and return list of dicts."""
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def async_query_one(
        db_path: Path,
        sql: str,
        params: tuple = (),
    ) -> Optional[dict]:
        """Execute a SELECT query and return the first row or None."""
        rows = await async_query(db_path, sql, params)
        return rows[0] if rows else None

except ImportError:
    # Fallback sync implementations wrapped in async signatures
    import asyncio

    async def async_query(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:  # type: ignore[misc]
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    async def async_query_one(db_path: Path, sql: str, params: tuple = ()) -> Optional[dict]:  # type: ignore[misc]
        rows = await async_query(db_path, sql, params)
        return rows[0] if rows else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def json_col(row: dict, col: str, default: Any = None) -> Any:
    """Safely parse a JSON column from a db row."""
    val = row.get(col)
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
