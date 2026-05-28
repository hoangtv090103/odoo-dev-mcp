"""
Phase 7: Cross-references and FTS5.

- Resolve related_field_paths: walk related='a.b.c' chains
- Populate search_index FTS5 table
- Update methods.is_cron_target for methods referenced in cron_jobs
- Update actions.res_model if missing (from binding_model)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


# ── Related field path resolution ────────────────────────────────────────────

def _resolve_related_paths(conn: sqlite3.Connection) -> None:
    """
    For each field with a related= value, walk the chain and insert
    a row into related_field_paths.

    Example: related='partner_id.country_id.name' on 'sale.order'
      → step_1_model='res.partner' (via partner_id's comodel_name)
      → step_2_model='res.country' (via country_id's comodel_name)
      → terminal_field='name' on 'res.country'
    """
    # Fetch all fields with a related value
    rows = conn.execute(
        """
        SELECT model_name, field_name, related, module_name
        FROM fields
        WHERE related IS NOT NULL AND related != ''
        """
    ).fetchall()

    logger.debug("Phase7: resolving %d related field paths", len(rows))

    # Build a lookup: (model_name, field_name) -> (comodel_name, field_type)
    field_lookup: dict[tuple[str, str], tuple[Optional[str], str]] = {}
    field_rows = conn.execute(
        "SELECT model_name, field_name, comodel_name, field_type FROM fields"
    ).fetchall()
    for fr in field_rows:
        field_lookup[(fr[0], fr[1])] = (fr[2], fr[3])

    for row in rows:
        source_model: str = row[0]
        source_field: str = row[1]
        related: str = row[2]
        module_name: str = row[3] or ""

        parts = related.split(".")
        if len(parts) < 2:
            continue

        # Walk the path
        step_1_model: Optional[str] = None
        step_1_field: Optional[str] = None
        step_2_model: Optional[str] = None
        step_2_field: Optional[str] = None
        terminal_model: Optional[str] = None
        terminal_field: Optional[str] = None
        terminal_type: Optional[str] = None
        fully_resolved = 0
        broken_at: Optional[str] = None

        current_model = source_model
        for i, part in enumerate(parts):
            lookup_key = (current_model, part)
            field_data = field_lookup.get(lookup_key)

            if i == len(parts) - 1:
                # Terminal field
                terminal_model = current_model
                terminal_field = part
                if field_data:
                    terminal_type = field_data[1]
                    fully_resolved = 1
                else:
                    broken_at = f"{current_model}.{part}"
                break

            # Intermediate field — must be relational
            if field_data is None:
                broken_at = f"{current_model}.{part}"
                # Record partial progress
                terminal_field = part
                terminal_model = current_model
                break

            comodel, ftype = field_data
            if i == 0:
                step_1_model = current_model
                step_1_field = part
                next_model = comodel
            elif i == 1:
                step_2_model = current_model
                step_2_field = part
                next_model = comodel
            else:
                next_model = comodel

            if not next_model:
                broken_at = f"{current_model}.{part} (no comodel)"
                terminal_model = current_model
                terminal_field = part
                break

            current_model = next_model

        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO related_field_paths
                    (source_model, source_field, path,
                     step_1_model, step_1_field,
                     step_2_model, step_2_field,
                     terminal_model, terminal_field, terminal_type,
                     fully_resolved, broken_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_model,
                    source_field,
                    related,
                    step_1_model,
                    step_1_field,
                    step_2_model,
                    step_2_field,
                    terminal_model,
                    terminal_field,
                    terminal_type,
                    fully_resolved,
                    broken_at,
                ),
            )
        except Exception as exc:
            logger.debug(
                "Phase7: related_field_paths insert error %s.%s: %s",
                source_model, source_field, exc,
            )


# ── Cron target update ────────────────────────────────────────────────────────

def _update_cron_targets(conn: sqlite3.Connection) -> None:
    """Mark methods that are referenced as cron targets."""
    conn.execute(
        """
        UPDATE methods
        SET is_cron_target = 1
        WHERE EXISTS (
            SELECT 1 FROM cron_jobs c
            WHERE c.model_name = methods.model_name
              AND c.method_name = methods.method_name
        )
        """
    )


# ── Action model resolution ───────────────────────────────────────────────────

def _resolve_action_models(conn: sqlite3.Connection) -> None:
    """
    Update actions.res_model where it is NULL but binding_model is set.
    This handles server actions bound to a model.
    """
    conn.execute(
        """
        UPDATE actions
        SET res_model = binding_model
        WHERE res_model IS NULL
          AND binding_model IS NOT NULL
          AND binding_model != ''
        """
    )


# ── FTS5 population ───────────────────────────────────────────────────────────

def _populate_fts(conn: sqlite3.Connection) -> None:
    """
    Populate the search_index FTS5 virtual table.

    Entity types:
      - 'model': model_name as entity_name, description as content
      - 'field': field_name as entity_name, model_name as model_context,
                 string_label + help_text as content
      - 'method': method_name as entity_name, model_name as model_context,
                  decorator_types as content
      - 'route': route_pattern as entity_name, auth+type as content
      - 'view': xml_id/name as entity_name, model as model_context
    """
    # Clear existing FTS data (for idempotent re-runs)
    try:
        conn.execute("DELETE FROM search_index")
    except Exception:
        pass

    # ── Models ────────────────────────────────────────────────────────────
    model_rows = conn.execute(
        """
        SELECT name, module_name, description, python_class
        FROM models
        WHERE name IS NOT NULL
        """
    ).fetchall()

    for row in model_rows:
        content_parts = []
        if row[2]:
            content_parts.append(row[2])
        if row[3]:
            content_parts.append(row[3])
        try:
            conn.execute(
                """
                INSERT INTO search_index
                    (entity_type, entity_name, model_context, content, module_name)
                VALUES ('model', ?, ?, ?, ?)
                """,
                (row[0], row[0], " ".join(content_parts), row[1] or ""),
            )
        except Exception as exc:
            logger.debug("Phase7: FTS model insert error %s: %s", row[0], exc)

    # ── Fields ────────────────────────────────────────────────────────────
    field_rows = conn.execute(
        """
        SELECT model_name, field_name, string_label, help_text, module_name, field_type
        FROM fields
        WHERE field_name IS NOT NULL
        """
    ).fetchall()

    for row in field_rows:
        content_parts = []
        if row[2]:
            content_parts.append(row[2])
        if row[3]:
            content_parts.append(row[3])
        if row[5]:
            content_parts.append(row[5])
        try:
            conn.execute(
                """
                INSERT INTO search_index
                    (entity_type, entity_name, model_context, content, module_name)
                VALUES ('field', ?, ?, ?, ?)
                """,
                (row[1], row[0], " ".join(content_parts), row[4] or ""),
            )
        except Exception as exc:
            logger.debug("Phase7: FTS field insert error %s.%s: %s", row[0], row[1], exc)

    # ── Methods ───────────────────────────────────────────────────────────
    method_rows = conn.execute(
        """
        SELECT model_name, method_name, decorator_types, module_name
        FROM methods
        WHERE method_name IS NOT NULL
        """
    ).fetchall()

    for row in method_rows:
        dec_types_raw = row[2] or "[]"
        try:
            dec_types = json.loads(dec_types_raw)
            content = " ".join(dec_types) if isinstance(dec_types, list) else str(dec_types_raw)
        except Exception:
            content = str(dec_types_raw)
        try:
            conn.execute(
                """
                INSERT INTO search_index
                    (entity_type, entity_name, model_context, content, module_name)
                VALUES ('method', ?, ?, ?, ?)
                """,
                (row[1], row[0], content, row[3] or ""),
            )
        except Exception as exc:
            logger.debug("Phase7: FTS method insert error %s.%s: %s", row[0], row[1], exc)

    # ── Routes ────────────────────────────────────────────────────────────
    route_rows = conn.execute(
        """
        SELECT route_pattern, auth, route_type, module_name, controller_class, method_name
        FROM http_routes
        WHERE route_pattern IS NOT NULL
        """
    ).fetchall()

    for row in route_rows:
        content_parts = []
        if row[1]:
            content_parts.append(f"auth:{row[1]}")
        if row[2]:
            content_parts.append(f"type:{row[2]}")
        if row[4]:
            content_parts.append(row[4])
        if row[5]:
            content_parts.append(row[5])
        try:
            conn.execute(
                """
                INSERT INTO search_index
                    (entity_type, entity_name, model_context, content, module_name)
                VALUES ('route', ?, '', ?, ?)
                """,
                (row[0], " ".join(content_parts), row[3] or ""),
            )
        except Exception as exc:
            logger.debug("Phase7: FTS route insert error %s: %s", row[0], exc)

    # ── Views ─────────────────────────────────────────────────────────────
    view_rows = conn.execute(
        """
        SELECT xml_id, name, model, view_type, module_name
        FROM views
        WHERE xml_id IS NOT NULL OR name IS NOT NULL
        """
    ).fetchall()

    for row in view_rows:
        entity_name = row[0] or row[1] or ""
        content_parts = []
        if row[1]:
            content_parts.append(row[1])
        if row[3]:
            content_parts.append(row[3])
        try:
            conn.execute(
                """
                INSERT INTO search_index
                    (entity_type, entity_name, model_context, content, module_name)
                VALUES ('view', ?, ?, ?, ?)
                """,
                (entity_name, row[2] or "", " ".join(content_parts), row[4] or ""),
            )
        except Exception as exc:
            logger.debug("Phase7: FTS view insert error %s: %s", entity_name, exc)


# ── Entry point ────────────────────────────────────────────────────

def run_crossref(conn: sqlite3.Connection) -> None:
    """
    Phase 7: Cross-references and FTS5.

    - Resolve related_field_paths
    - Populate search_index FTS5 table
    - Update methods.is_cron_target
    - Update actions.res_model from binding_model
    """
    logger.info("Phase 7: cross-references and FTS5")

    # Resolve related field chains
    try:
        _resolve_related_paths(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase7: related_paths error: %s", exc)

    # Mark cron target methods
    try:
        _update_cron_targets(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase7: cron_targets error: %s", exc)

    # Resolve action models from binding_model
    try:
        _resolve_action_models(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase7: action_model resolution error: %s", exc)

    # Populate FTS5
    try:
        _populate_fts(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase7: FTS5 population error: %s", exc)

    logger.info("Phase 7: complete")
