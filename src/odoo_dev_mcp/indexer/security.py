"""
Phase 5: Security rules.

Focuses on:
  - Parsing ir.model.access.csv files with parse_csv_access()
  - Ensuring completeness of access_rules and record_rules tables
  - Inserting field_groups_map from view_element_refs (groups_attr on field elements)

Note: XML-sourced access_rules and record_rules are already inserted by the crosslayer step.
Phase 5 avoids double-inserting those; it only adds CSV-sourced access rules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from ..parsers.xml_parser import parse_csv_access
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── CSV access rule insertion ─────────────────────────────────────────────────

def _insert_csv_access_rules(
    conn: sqlite3.Connection,
    module: ModuleRecord,
) -> None:
    """Parse and insert all ir.model.access.csv files for a module."""
    for csv_file in module.csv_files:
        try:
            rules = parse_csv_access(csv_file, module.name)
        except Exception as exc:
            logger.warning("Phase5: error parsing CSV %s: %s", csv_file, exc)
            continue

        for rule in rules:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO access_rules
                        (xml_id, name, model_name, group_xml_id,
                         perm_read, perm_write, perm_create, perm_unlink,
                         module_name, file_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.xml_id,
                        rule.name,
                        rule.model_name,
                        rule.group_xml_id,
                        rule.perm_read,
                        rule.perm_write,
                        rule.perm_create,
                        rule.perm_unlink,
                        rule.module_name,
                        rule.file_path,
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "Phase5: access_rule insert error for %s: %s", rule.name, exc
                )


# ── Field groups from view_element_refs ───────────────────────────────────────

def _extract_field_groups_from_views(conn: sqlite3.Connection) -> None:
    """
    Read view_element_refs where groups_attr is set on field elements,
    then insert into field_groups_map with source='view_attr'.

    We also need the model context for each view, which comes from views.model.
    """
    # Get all field elements with groups from view_element_refs, joined to views
    rows = conn.execute(
        """
        SELECT ver.view_xml_id, ver.field_name, ver.groups_attr,
               v.model, v.module_name
        FROM view_element_refs ver
        JOIN views v ON v.xml_id = ver.view_xml_id
        WHERE ver.element_type = 'field'
          AND ver.groups_attr IS NOT NULL
          AND ver.groups_attr != ''
          AND ver.field_name IS NOT NULL
          AND v.model IS NOT NULL
        """
    ).fetchall()

    inserted = 0
    for row in rows:
        model_name = row[3]
        field_name = row[1]
        groups_str = row[2]
        module_name = row[4] or ""

        for group in [g.strip() for g in groups_str.split(",") if g.strip()]:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO field_groups_map
                        (model_name, field_name, group_xml_id, source, module_name)
                    VALUES (?, ?, ?, 'view_attr', ?)
                    """,
                    (model_name, field_name, group, module_name),
                )
                inserted += 1
            except Exception as exc:
                logger.debug("Phase5: field_groups_map insert error: %s", exc)

    logger.debug("Phase5: inserted %d field_groups_map rows from view_element_refs", inserted)


# ── Completeness check for record_rules ──────────────────────────────────────

def _ensure_record_rules_complete(
    conn: sqlite3.Connection,
    modules: list[ModuleRecord],
) -> None:
    """
    Re-parse XML for record_rules that might have been missed by the crosslayer step
    (e.g., files not in standard locations).

    This is a lightweight pass — only processes files not yet covered.
    """
    from ..parsers.xml_parser import parse_xml_file

    # Get already-inserted record_rule file paths to avoid duplication
    existing_files: set[str] = set()
    rows = conn.execute("SELECT DISTINCT file_path FROM record_rules WHERE file_path IS NOT NULL").fetchall()
    for row in rows:
        existing_files.add(row[0])

    for module in modules:
        for xml_file in module.xml_files:
            file_str = str(xml_file)
            if file_str in existing_files:
                continue
            # Only process security-related files that might contain rules
            fname_lower = xml_file.name.lower()
            if not any(
                keyword in fname_lower or keyword in str(xml_file.parent).lower()
                for keyword in ("security", "rule", "access")
            ):
                continue
            try:
                result = parse_xml_file(xml_file, module.name)
                for rr in result.record_rules:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO record_rules
                            (xml_id, name, model_name, domain_force, groups,
                             perm_read, perm_write, perm_create, perm_unlink,
                             module_name, file_path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rr.xml_id,
                            rr.name,
                            rr.model_name,
                            rr.domain_force,
                            json.dumps(rr.groups),
                            rr.perm_read,
                            rr.perm_write,
                            rr.perm_create,
                            rr.perm_unlink,
                            rr.module_name,
                            rr.file_path,
                        ),
                    )
            except Exception as exc:
                logger.debug("Phase5: record_rule secondary parse error %s: %s", xml_file, exc)


# ── Entry point ────────────────────────────────────────────────────

def run_security(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 5: Security rules.

    - Parse ir.model.access.csv files
    - Extract field_groups_map from view_element_refs
    - Ensure record_rules completeness for security files
    """
    logger.info("Phase 5: security rules for %d modules", len(modules))

    # CSV access rules
    for module in modules:
        try:
            _insert_csv_access_rules(conn, module)
        except Exception as exc:
            logger.warning("Phase5: CSV error for module %s: %s", module.name, exc)

    conn.commit()

    # Field groups from view attributes
    try:
        _extract_field_groups_from_views(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase5: field_groups extraction error: %s", exc)

    # Record rules completeness pass
    try:
        _ensure_record_rules_complete(conn, modules)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase5: record_rules completeness error: %s", exc)

    logger.info("Phase 5: complete")
