"""Global project registry — persisted in ``~/.local/share/odoo-dev-mcp/registry.db``."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import ProjectConfig, get_data_dir, get_registry_path


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    root_path    TEXT NOT NULL,
    config_path  TEXT,
    db_path      TEXT NOT NULL,
    addons_hash  TEXT NOT NULL,
    last_indexed REAL,
    is_active    INTEGER DEFAULT 0,
    created_at   REAL,
    notes        TEXT
);
"""

_DDL_ADDONS_PATHS = """
CREATE TABLE IF NOT EXISTS project_addons_paths (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    label      TEXT,
    sequence   INTEGER DEFAULT 10
);
"""

_DDL_PRAGMA = "PRAGMA foreign_keys = ON;"


# ---------------------------------------------------------------------------
# Data transfer object
# ---------------------------------------------------------------------------

@dataclass
class ProjectRegistryEntry:
    """In-memory representation of a row in the ``projects`` table."""

    name: str
    root_path: str
    config_path: Optional[str]
    db_path: str
    addons_hash: str
    last_indexed: Optional[float]
    is_active: bool
    created_at: Optional[float]
    addons_paths: list[dict] = field(default_factory=list)
    """Each element: ``{"path": str, "label": str, "sequence": int}``"""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ProjectRegistry:
    """CRUD interface over the global SQLite project registry.

    Usage::

        registry = ProjectRegistry()
        registry.add(project_config)
        for entry in registry.list_all():
            print(entry.name)
    """

    def __init__(self, registry_path: Optional[Path] = None) -> None:
        self._path: Path = registry_path or get_registry_path()
        # Ensure the parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute(_DDL_PRAGMA)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Create tables if they do not already exist."""
        conn.execute(_DDL_PROJECTS)
        conn.execute(_DDL_ADDONS_PATHS)
        conn.commit()

    def _addons_paths_for(
        self, conn: sqlite3.Connection, project_id: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT path, label, sequence FROM project_addons_paths "
            "WHERE project_id = ? ORDER BY sequence, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_entry(
        row: sqlite3.Row, addons_paths: list[dict]
    ) -> ProjectRegistryEntry:
        return ProjectRegistryEntry(
            name=row["name"],
            root_path=row["root_path"],
            config_path=row["config_path"],
            db_path=row["db_path"],
            addons_hash=row["addons_hash"],
            last_indexed=row["last_indexed"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            addons_paths=addons_paths,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, config: ProjectConfig) -> None:
        """Insert or replace a project derived from *config*.

        Existing ``addons_paths`` rows for the project are replaced.
        ``created_at`` is preserved when updating an existing record.
        """
        with self._connect() as conn:
            self._ensure_schema(conn)
            now = time.time()

            # Preserve existing created_at if the record already exists
            existing = conn.execute(
                "SELECT id, created_at FROM projects WHERE name = ?",
                (config.name,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now

            conn.execute(
                """
                INSERT INTO projects
                    (name, root_path, config_path, db_path, addons_hash,
                     last_indexed, is_active, created_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    root_path   = excluded.root_path,
                    config_path = excluded.config_path,
                    db_path     = excluded.db_path,
                    addons_hash = excluded.addons_hash,
                    created_at  = excluded.created_at
                """,
                (
                    config.name,
                    str(config.root_path),
                    str(config.config_file_path) if config.config_file_path else None,
                    str(config.db_path),
                    config.addons_hash,
                    None,  # last_indexed — untouched here
                    0,
                    created_at,
                    None,
                ),
            )
            project_id: int = conn.execute(
                "SELECT id FROM projects WHERE name = ?", (config.name,)
            ).fetchone()["id"]

            # Replace addons path rows
            conn.execute(
                "DELETE FROM project_addons_paths WHERE project_id = ?",
                (project_id,),
            )
            for seq, entry in enumerate(config.addons_paths, start=10):
                conn.execute(
                    "INSERT INTO project_addons_paths (project_id, path, label, sequence) "
                    "VALUES (?, ?, ?, ?)",
                    (project_id, str(entry.path), entry.label or None, seq),
                )
            conn.commit()

    def remove(self, name: str) -> None:
        """Delete a project by *name* (cascades to addons_paths)."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            conn.commit()

    def list_all(self) -> list[ProjectRegistryEntry]:
        """Return all registered projects ordered by name."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY name"
            ).fetchall()
            result: list[ProjectRegistryEntry] = []
            for row in rows:
                paths = self._addons_paths_for(conn, row["id"])
                result.append(self._row_to_entry(row, paths))
            return result

    def get(self, name: str) -> Optional[ProjectRegistryEntry]:
        """Return the entry for *name*, or ``None`` if not found."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            paths = self._addons_paths_for(conn, row["id"])
            return self._row_to_entry(row, paths)

    def set_active(self, name: str) -> None:
        """Mark *name* as the active project (clears all other active flags)."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("UPDATE projects SET is_active = 0")
            conn.execute(
                "UPDATE projects SET is_active = 1 WHERE name = ?", (name,)
            )
            conn.commit()

    def get_active(self) -> Optional[ProjectRegistryEntry]:
        """Return the currently active project, or ``None``."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM projects WHERE is_active = 1 LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            paths = self._addons_paths_for(conn, row["id"])
            return self._row_to_entry(row, paths)

    def update_last_indexed(self, name: str) -> None:
        """Stamp ``last_indexed`` with the current wall-clock time."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                "UPDATE projects SET last_indexed = ? WHERE name = ?",
                (time.time(), name),
            )
            conn.commit()

    def find_by_path(self, path: str) -> Optional[ProjectRegistryEntry]:
        """Return the first project whose addons paths contain *path*.

        Matching is done by checking whether *path* starts with any of the
        registered addons directory paths, after normalising both sides.
        """
        needle = str(Path(path).resolve())
        with self._connect() as conn:
            self._ensure_schema(conn)
            # Fetch all project_id values whose addons paths are a prefix of needle
            rows = conn.execute(
                """
                SELECT DISTINCT p.*
                FROM projects p
                JOIN project_addons_paths ap ON ap.project_id = p.id
                WHERE ? LIKE ap.path || '%'
                ORDER BY length(ap.path) DESC
                LIMIT 1
                """,
                (needle,),
            ).fetchall()
            if not rows:
                return None
            row = rows[0]
            ap_paths = self._addons_paths_for(conn, row["id"])
            return self._row_to_entry(row, ap_paths)
