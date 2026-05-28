"""
Phase 4: Cross-layer XML analysis.

Parses all .xml files using parse_xml_file() and inserts:
  - views + view_element_refs
  - actions (act_window, server, client, report)
  - menus
  - cron_jobs
  - email_templates
  - qweb_templates

After all inserts, performs a second pass to resolve
menus.res_model via actions.res_model using action_xml_id.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from ..parsers.xml_parser import (
    XmlFileResult,
    ViewRecord,
    ViewElementRef,
    ActionRecord,
    MenuRecord,
    CronRecord,
    EmailTemplateRecord,
    QwebTemplateRecord,
    parse_xml_file,
)
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── View insertion ────────────────────────────────────────────────────────────

def _insert_view(conn: sqlite3.Connection, v: ViewRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO views
            (xml_id, name, model, view_type, inherit_id, priority,
             field_names, button_names, view_group,
             module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            v.xml_id,
            v.name,
            v.model,
            v.view_type,
            v.inherit_id,
            v.priority,
            json.dumps(v.arch_field_names),
            json.dumps(v.arch_button_names),
            v.view_group,
            v.module_name,
            v.file_path,
        ),
    )


def _insert_view_elements(conn: sqlite3.Connection, elements: list[ViewElementRef]) -> None:
    for el in elements:
        try:
            conn.execute(
                """
                INSERT INTO view_element_refs
                    (view_xml_id, element_type,
                     field_name, widget_name, field_options, field_attrs,
                     button_name, button_type, button_action, button_confirm,
                     button_states, button_groups,
                     xpath_expr, filter_domain, filter_fields,
                     groups_attr, invisible_expr, attrs_expr,
                     parent_element, depth)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    el.view_xml_id,
                    el.element_type,
                    el.field_name,
                    el.widget_name,
                    el.field_options,
                    el.field_attrs,
                    el.button_name,
                    el.button_type,
                    el.button_action,
                    el.button_confirm,
                    el.button_states,
                    el.button_groups,
                    el.xpath_expr,
                    el.filter_domain,
                    json.dumps(el.filter_fields),
                    el.groups_attr,
                    el.invisible_expr,
                    el.attrs_expr,
                    el.parent_element,
                    el.depth,
                ),
            )
        except Exception as exc:
            logger.debug("Phase4: view_element_ref insert error: %s", exc)


# ── Action insertion ──────────────────────────────────────────────────────────

def _insert_action(conn: sqlite3.Connection, a: ActionRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO actions
            (xml_id, action_type, name, res_model, view_mode, domain,
             context_expr, target, binding_model, binding_views,
             server_method, tag, report_name, report_model,
             domain_fields, module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            a.xml_id,
            a.action_type,
            a.name,
            a.res_model,
            a.view_mode,
            a.domain,
            a.context_expr,
            a.target,
            a.binding_model,
            json.dumps(a.binding_views),
            a.server_method,
            a.tag,
            a.report_name,
            a.report_model,
            json.dumps(a.domain_fields),
            a.module_name,
            a.file_path,
        ),
    )


# ── Menu insertion ────────────────────────────────────────────────────────────

def _insert_menu(conn: sqlite3.Connection, m: MenuRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO menus
            (xml_id, name, parent_xml_id, sequence, action_xml_id,
             action_type, res_model, groups, web_icon,
             module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            m.xml_id,
            m.name,
            m.parent_xml_id,
            m.sequence,
            m.action_xml_id,
            m.action_type,
            m.res_model,
            json.dumps(m.groups),
            m.web_icon,
            m.module_name,
            m.file_path,
        ),
    )


# ── Cron insertion ────────────────────────────────────────────────────────────

def _insert_cron(conn: sqlite3.Connection, c: CronRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO cron_jobs
            (xml_id, name, model_name, method_name, method_args,
             interval_number, interval_type, numbercall, doall,
             priority, active, module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            c.xml_id,
            c.name,
            c.model_name,
            c.method_name,
            c.method_args,
            c.interval_number,
            c.interval_type,
            c.numbercall,
            c.doall,
            c.priority,
            c.active,
            c.module_name,
            c.file_path,
        ),
    )


# ── Email template insertion ──────────────────────────────────────────────────

def _insert_email_template(conn: sqlite3.Connection, e: EmailTemplateRecord) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO email_templates
            (xml_id, name, model_name, subject, body_field_refs,
             email_from, email_to, reply_to, report_template,
             module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            e.xml_id,
            e.name,
            e.model_name,
            e.subject,
            json.dumps(e.body_field_refs),
            e.email_from,
            e.email_to,
            e.reply_to,
            e.report_template,
            e.module_name,
            e.file_path,
        ),
    )


# ── QWeb template insertion ───────────────────────────────────────────────────

def _insert_qweb_template(conn: sqlite3.Connection, q: QwebTemplateRecord) -> None:
    is_primary = 1 if not q.inherit_id else 0
    conn.execute(
        """
        INSERT OR IGNORE INTO qweb_templates
            (xml_id, name, inherit_id, priority, is_primary,
             template_type, report_model,
             t_calls, t_fields, t_if_fields,
             module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            q.xml_id,
            q.name,
            q.inherit_id,
            q.priority,
            is_primary,
            q.template_type,
            q.report_model,
            json.dumps(q.t_calls),
            json.dumps(q.t_fields),
            json.dumps(q.t_if_fields),
            q.module_name,
            q.file_path,
        ),
    )


# ── Access rule insertion ─────────────────────────────────────────────────────

def _insert_access_rule_from_xml(conn: sqlite3.Connection, ar) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO access_rules
            (xml_id, name, model_name, group_xml_id,
             perm_read, perm_write, perm_create, perm_unlink,
             module_name, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ar.xml_id,
            ar.name,
            ar.model_name,
            ar.group_xml_id,
            ar.perm_read,
            ar.perm_write,
            ar.perm_create,
            ar.perm_unlink,
            ar.module_name,
            ar.file_path,
        ),
    )


def _insert_record_rule(conn: sqlite3.Connection, rr) -> None:
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


# ── Post-insert menu resolution ───────────────────────────────────────────────

def _resolve_menu_models(conn: sqlite3.Connection) -> None:
    """
    Second pass: for each menu with an action_xml_id, look up the
    corresponding action's res_model and update menus.res_model.
    """
    conn.execute(
        """
        UPDATE menus
        SET res_model = (
            SELECT a.res_model
            FROM actions a
            WHERE a.xml_id = menus.action_xml_id
              AND a.res_model IS NOT NULL
            LIMIT 1
        )
        WHERE res_model IS NULL
          AND action_xml_id IS NOT NULL
        """
    )

    # Also update menus.action_type from actions table
    conn.execute(
        """
        UPDATE menus
        SET action_type = (
            SELECT a.action_type
            FROM actions a
            WHERE a.xml_id = menus.action_xml_id
            LIMIT 1
        )
        WHERE action_type IS NULL
          AND action_xml_id IS NOT NULL
        """
    )


# ── Result processor ──────────────────────────────────────────────────────────

def _process_xml_result(
    conn: sqlite3.Connection,
    result: XmlFileResult,
) -> None:
    """Insert all records from one XmlFileResult into the database."""
    for v in result.views:
        try:
            _insert_view(conn, v)
            if v.elements:
                _insert_view_elements(conn, v.elements)
        except Exception as exc:
            logger.debug("Phase4: view insert error for %s: %s", v.xml_id, exc)

    for a in result.actions:
        try:
            _insert_action(conn, a)
        except Exception as exc:
            logger.debug("Phase4: action insert error for %s: %s", a.xml_id, exc)

    for m in result.menus:
        try:
            _insert_menu(conn, m)
        except Exception as exc:
            logger.debug("Phase4: menu insert error for %s: %s", m.xml_id, exc)

    for c in result.cron_jobs:
        try:
            _insert_cron(conn, c)
        except Exception as exc:
            logger.debug("Phase4: cron insert error for %s: %s", c.xml_id, exc)

    for e in result.email_templates:
        try:
            _insert_email_template(conn, e)
        except Exception as exc:
            logger.debug("Phase4: email_template insert error for %s: %s", e.xml_id, exc)

    for q in result.qweb_templates:
        try:
            _insert_qweb_template(conn, q)
        except Exception as exc:
            logger.debug("Phase4: qweb_template insert error for %s: %s", q.xml_id, exc)

    for ar in result.access_rules:
        try:
            _insert_access_rule_from_xml(conn, ar)
        except Exception as exc:
            logger.debug("Phase4: access_rule insert error: %s", exc)

    for rr in result.record_rules:
        try:
            _insert_record_rule(conn, rr)
        except Exception as exc:
            logger.debug("Phase4: record_rule insert error: %s", exc)


# ── Entry point ────────────────────────────────────────────────────

def run_crosslayer(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 4: Cross-layer XML analysis.

    Parses all XML files in each module and inserts views, actions,
    menus, cron_jobs, email_templates, qweb_templates, and security rules.
    Resolves menu→action→model chain after all inserts.
    """
    logger.info("Phase 4: XML cross-layer analysis for %d modules", len(modules))

    for module in modules:
        for xml_file in module.xml_files:
            try:
                result = parse_xml_file(xml_file, module.name)
                _process_xml_result(conn, result)
            except Exception as exc:
                logger.warning("Phase4: error parsing %s: %s", xml_file, exc)

    conn.commit()

    # Second pass: resolve menus
    try:
        _resolve_menu_models(conn)
        conn.commit()
    except Exception as exc:
        logger.warning("Phase4: menu resolution error: %s", exc)

    logger.info("Phase 4: complete")
