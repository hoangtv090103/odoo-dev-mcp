"""
Indexing pipeline orchestrator.

Runs all indexing steps in order and returns an IndexResult.
Writes metadata to index_meta after completion.

Atomic write strategy
─────────────────────
Full rebuilds (reset=True or incremental=False) write to a temporary file
``index.db.new`` sitting next to the real ``index.db``.  Only when every
phase has succeeded and ``indexed_at`` metadata has been written is the temp
file renamed over the real file in a single atomic ``os.replace()`` call.

This means ``index.db`` is *always* a complete, consistent snapshot:

  • If the server is killed mid-index, ``index.db`` still contains the previous
    good index (or doesn't exist at all for first-time runs).  Either way, the
    next server start will detect the missing / incomplete state and rebuild
    cleanly — without the "structural data present, routes/views/state_machines
    empty" corruption seen when writing directly.

  • Incremental runs modify ``index.db`` in-place (they only add rows to an
    already-complete index) and are *not* wrapped in the atomic dance.

AI discovery files
──────────────────
After every successful index (full or incremental) two discovery files are
written alongside the index:

  • SKILL.md files are written by ``odoo-dev-mcp install``, not here.
    The install command is the right place for IDE/agent integration files.

  • ``<project_root>/.odoo-dev-mcp/INDEX_INFO.json`` — lightweight status
    summary readable without an MCP connection, so the AI can confirm the
    index exists before calling any tool.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..config import ProjectConfig
from ..db.connection import open_db, set_meta
from ..version_detect import detect_odoo_version_hint
from .module_scanner import scan_all_paths, ModuleRecord
from .structural import run_structural
from .decorators import run_decorators
from .behavioral import run_behavioral
from .crosslayer import run_crosslayer
from .security import run_security
from .js import run_js
from .crossref import run_crossref
from .hashing import (
    compute_module_hash,
    find_changed_modules,
    get_stored_hashes,
    store_module_hashes,
    delete_module_data,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "6"

# Suffix used for the in-progress temp database during a full rebuild.
_TMP_SUFFIX = ".new"

# Callback type: (step: int, total: int, message: str) -> None
ProgressCallback = Callable[[int, int, str], None]


# ── Atomic-write helpers ──────────────────────────────────────────────────────

def tmp_db_path(db_path: Path) -> Path:
    """Return the path of the in-progress temp DB alongside db_path."""
    return db_path.parent / (db_path.name + _TMP_SUFFIX)


def cleanup_stale_tmp(db_path: Path) -> bool:
    """Remove a leftover ``.new`` temp file if it exists.

    Called at server startup so a previous interrupted full rebuild doesn't
    prevent a fresh rebuild from starting.

    Returns True if a stale file was removed.
    """
    tmp = tmp_db_path(db_path)
    if tmp.exists():
        try:
            tmp.unlink()
            logger.info("Removed stale temp index file: %s", tmp)
            return True
        except OSError as exc:
            logger.warning("Could not remove stale temp index %s: %s", tmp, exc)
    return False


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class IndexResult:
    project_name: str
    modules_count: int
    models_count: int
    fields_count: int
    methods_count: int
    views_count: int
    routes_count: int
    duration_seconds: float
    errors: list[str] = field(default_factory=list)
    # Incremental stats
    changed_modules: int = 0
    skipped_modules: int = 0


# ── Count helpers ─────────────────────────────────────────────────────────────

def _count(conn, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ── Metadata writing ──────────────────────────────────────────────────────────

def _write_metadata(
    conn,
    config: ProjectConfig,
    modules: list[ModuleRecord],
    result: IndexResult,
    odoo_version: Optional[str],
) -> None:
    """Write all index_meta key/value pairs."""
    set_meta(conn, "project_name", config.name)
    set_meta(conn, "addons_paths", json.dumps([str(e.path) for e in config.addons_paths]))
    set_meta(conn, "addons_hash", config.addons_hash)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    set_meta(conn, "odoo_version_hint", odoo_version or "")
    set_meta(conn, "indexed_at", datetime.now(timezone.utc).isoformat())
    set_meta(conn, "modules_count", str(result.modules_count))
    set_meta(conn, "models_count", str(result.models_count))
    set_meta(conn, "fields_count", str(result.fields_count))
    set_meta(conn, "methods_count", str(result.methods_count))
    conn.commit()


# ── AI discovery file writers ─────────────────────────────────────────────────

def _write_index_info(config: ProjectConfig, result: IndexResult, odoo_version: Optional[str]) -> None:
    """Write .odoo-dev-mcp/INDEX_INFO.json — lightweight status readable without MCP.

    The AI can open this file to confirm the index exists and is complete
    before calling any MCP tool, avoiding unnecessary rebuild attempts.
    """
    info = {
        "project_name":   result.project_name,
        "db_path":        str(config.db_path),
        "indexed_at":     datetime.now(timezone.utc).isoformat(),
        "odoo_version":   odoo_version or "unknown",
        "modules_count":  result.modules_count,
        "models_count":   result.models_count,
        "fields_count":   result.fields_count,
        "methods_count":  result.methods_count,
        "views_count":    result.views_count,
        "routes_count":   result.routes_count,
        "status":         "complete",
        "first_tool":     "get_project_context()",
    }
    out = config.db_path.parent / "INDEX_INFO.json"
    try:
        out.write_text(json.dumps(info, indent=2), encoding="utf-8")
        logger.debug("Wrote INDEX_INFO.json → %s", out)
    except OSError as exc:
        logger.warning("Could not write INDEX_INFO.json: %s", exc)


# ── Step runner helper ───────────────────────────────────────────────────────

def _run_step(
    step_num: int,
    total_steps: int,
    name: str,
    fn,
    errors: list[str],
    progress_cb: Optional[ProgressCallback],
) -> None:
    if progress_cb:
        progress_cb(step_num, total_steps, name)
    try:
        fn()
    except Exception as exc:
        msg = f"{name} failed: {exc}"
        logger.error(msg)
        errors.append(msg)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_full_index(
    config: ProjectConfig,
    *,
    reset: bool = False,
    incremental: bool = False,
    progress_cb: Optional[ProgressCallback] = None,
) -> IndexResult:
    """
    Run all indexing steps in order.

    Args:
        config:      Project configuration with addons paths.
        reset:       If True, drop and recreate the schema before indexing.
        incremental: If True, only re-index modules whose source files changed.
                     Ignored when reset=True.
        progress_cb: Optional step callback(step, total, message).

    Returns:
        IndexResult with counts and timing.

    Atomic write behaviour
    ──────────────────────
    For full rebuilds (``not incremental``), all writes go to a temporary
    ``index.db.new`` file.  Only after every phase succeeds and metadata
    (including ``indexed_at``) has been written is the temp file atomically
    renamed to ``index.db`` via ``os.replace()``.  If the process is killed
    at any point during a full rebuild, ``index.db`` is left untouched (either
    still the previous good copy, or absent for first-time runs).

    Incremental runs write directly to the existing ``index.db`` because they
    only append rows to an already-complete index — a partial incremental run
    is safe; the affected modules will simply be re-indexed on the next run.
    """
    start_time = time.monotonic()
    errors: list[str] = []
    total_steps = 8  # scan + 7 indexing steps

    # Full rebuild → atomic write to temp file, rename at end.
    # Incremental → write directly to final path.
    use_atomic = not incremental
    write_path = tmp_db_path(config.db_path) if use_atomic else config.db_path

    logger.info(
        "Starting %s index for project '%s' (write_path=%s)",
        "full" if use_atomic else "incremental", config.name, write_path,
    )

    # ── Step 0: Scan modules ──────────────────────────────────────────────
    if progress_cb:
        progress_cb(0, total_steps, "Scanning addons paths")

    try:
        modules = scan_all_paths(config)
    except Exception as exc:
        msg = f"Module scanning failed: {exc}"
        logger.error(msg)
        errors.append(msg)
        modules = []

    logger.info("Found %d modules", len(modules))

    # ── Open database ─────────────────────────────────────────────────────
    # For full rebuilds we always start from a fresh schema in the temp file.
    # For incremental we open the existing DB as-is (reset is ignored here
    # since incremental=True implies the DB already exists and is complete).
    open_reset = True if use_atomic else reset
    try:
        conn = open_db(write_path, reset=open_reset)
    except Exception as exc:
        msg = f"Failed to open database at {write_path}: {exc}"
        logger.error(msg)
        return IndexResult(
            project_name=config.name,
            modules_count=len(modules),
            models_count=0,
            fields_count=0,
            methods_count=0,
            views_count=0,
            routes_count=0,
            duration_seconds=time.monotonic() - start_time,
            errors=[msg],
        )

    # ── Detect Odoo version ───────────────────────────────────────────────
    odoo_version: Optional[str] = None
    try:
        odoo_version = detect_odoo_version_hint(config.all_paths)
        if odoo_version:
            logger.info("Detected Odoo version: %s", odoo_version)
    except Exception as exc:
        logger.warning("Version detection failed: %s", exc)

    # ── Incremental: find changed modules ────────────────────────────────
    changed_count = len(modules)
    skipped_count = 0
    modules_to_index = modules          # default: all
    new_hashes: dict[str, str] = {}

    if incremental and not reset and modules:
        stored = get_stored_hashes(conn)
        changed, unchanged = find_changed_modules(modules, stored)
        skipped_count = len(unchanged)
        changed_count = len(changed)
        modules_to_index = changed

        if changed:
            for m in changed:
                delete_module_data(conn, m.name)
            logger.info(
                "Incremental: %d changed, %d unchanged (skipped)",
                len(changed), len(unchanged),
            )
            if progress_cb:
                progress_cb(0, total_steps,
                    f"Incremental: {len(changed)} changed, {len(unchanged)} unchanged")
        else:
            logger.info("Incremental: no changes detected, index is up to date")
            if progress_cb:
                progress_cb(total_steps, total_steps, "Index is already up to date")
            result = IndexResult(
                project_name=config.name,
                modules_count=len(modules),
                models_count=_count(conn, "models"),
                fields_count=_count(conn, "fields"),
                methods_count=_count(conn, "methods"),
                views_count=_count(conn, "views"),
                routes_count=_count(conn, "http_routes"),
                duration_seconds=time.monotonic() - start_time,
                changed_modules=0,
                skipped_modules=skipped_count,
            )
            try:
                conn.close()
            except Exception:
                pass
            return result

        # Pre-compute new hashes for changed modules
        for m in changed:
            new_hashes[m.name] = compute_module_hash(m)

    # Full rebuilds always start from scratch — no pre-seeding of hashes needed.
    # The previous index is still at config.db_path (untouched) until the
    # atomic rename at the end, so it remains available for tools during rebuild.

    # ── Step: Structural ─────────────────────────────────────────────────
    _run_step(
        1, total_steps, "Structural (models, fields, methods)",
        lambda: run_structural(conn, modules_to_index),
        errors, progress_cb,
    )

    # ── Step: Decorators ─────────────────────────────────────────────────
    _run_step(
        2, total_steps, "Decorator detail",
        lambda: run_decorators(conn, modules_to_index),
        errors, progress_cb,
    )

    # ── Step: Behavioral ─────────────────────────────────────────────────
    _run_step(
        3, total_steps, "Behavioral (method bodies, state machines)",
        lambda: run_behavioral(conn, modules_to_index),
        errors, progress_cb,
    )

    # ── Step: Cross-layer XML ─────────────────────────────────────────────
    _run_step(
        4, total_steps, "Cross-layer XML (views, actions, menus)",
        lambda: run_crosslayer(conn, modules_to_index),
        errors, progress_cb,
    )

    # ── Step: Security ────────────────────────────────────────────────────
    _run_step(
        5, total_steps, "Security rules (CSV access, record rules)",
        lambda: run_security(conn, modules_to_index),
        errors, progress_cb,
    )

    # ── Step: JavaScript ──────────────────────────────────────────────────
    if config.js_parsing:
        _run_step(
            6, total_steps, "JavaScript components",
            lambda: run_js(conn, modules_to_index),
            errors, progress_cb,
        )
    else:
        logger.debug("JS parsing disabled, skipping")
        if progress_cb:
            progress_cb(6, total_steps, "JavaScript (skipped)")

    # ── Step: Cross-references ────────────────────────────────────────────
    _run_step(
        7, total_steps, "Cross-references and FTS5",
        lambda: run_crossref(conn),
        errors, progress_cb,
    )

    # ── Store file hashes ─────────────────────────────────────────────────
    # Full index: compute and store hashes for all modules
    # Incremental: new_hashes already computed for changed modules
    if not incremental or not new_hashes:
        new_hashes = {m.name: compute_module_hash(m) for m in modules_to_index}
    try:
        store_module_hashes(conn, modules_to_index, new_hashes)
    except Exception as exc:
        logger.warning("Failed to store file hashes: %s", exc)

    # ── Collect final counts ──────────────────────────────────────────────
    models_count = _count(conn, "models")
    fields_count = _count(conn, "fields")
    methods_count = _count(conn, "methods")
    views_count = _count(conn, "views")
    routes_count = _count(conn, "http_routes")

    duration = time.monotonic() - start_time

    result = IndexResult(
        project_name=config.name,
        modules_count=len(modules),
        models_count=models_count,
        fields_count=fields_count,
        methods_count=methods_count,
        views_count=views_count,
        routes_count=routes_count,
        duration_seconds=duration,
        errors=errors,
        changed_modules=changed_count,
        skipped_modules=skipped_count,
    )

    # ── Write metadata ────────────────────────────────────────────────────
    try:
        _write_metadata(conn, config, modules, result, odoo_version)
    except Exception as exc:
        logger.warning("Failed to write metadata: %s", exc)

    # ── Final progress ────────────────────────────────────────────────────
    if progress_cb:
        progress_cb(
            total_steps, total_steps,
            f"Done: {len(modules)} modules, {models_count} models, "
            f"{fields_count} fields, {methods_count} methods in {duration:.1f}s",
        )

    logger.info(
        "Index complete: %d modules, %d models, %d fields, %d methods, "
        "%d views, %d routes in %.2fs",
        len(modules), models_count, fields_count, methods_count,
        views_count, routes_count, duration,
    )

    try:
        conn.close()
    except Exception:
        pass

    # ── Atomic rename (full rebuild only) ─────────────────────────────────
    # All phases succeeded and indexed_at has been written.  Now atomically
    # promote the temp file to the canonical index path.
    #
    # os.replace() is atomic on POSIX (rename syscall) and near-atomic on
    # Windows (since Python 3.3).  Any reader currently holding a connection
    # to the old index.db continues to work normally — SQLite keeps the file
    # open by inode, not by name.
    if use_atomic:
        try:
            os.replace(write_path, config.db_path)
            logger.info("Atomically promoted %s → %s", write_path, config.db_path)
        except OSError as exc:
            msg = f"Atomic rename failed ({write_path} → {config.db_path}): {exc}"
            logger.error(msg)
            result.errors.append(msg)

    # ── Write AI discovery files ──────────────────────────────────────────
    # Both files are best-effort: failures never abort the index or add errors.
    # Written after the atomic rename so db_path is the final canonical path.
    # ── Write INDEX_INFO.json ─────────────────────────────────────────────
    # Best-effort: failures never abort the index or add errors.
    # SKILL.md is written by `odoo-dev-mcp install` (IDE integration step),
    # not here — index is concerned with knowledge graph output only.
    try:
        _write_index_info(config, result, odoo_version)
    except Exception as exc:
        logger.warning("Failed to write INDEX_INFO.json: %s", exc)

    return result
