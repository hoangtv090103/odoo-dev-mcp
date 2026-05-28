"""Tool 09: get_model_actions — actions, menus, cron jobs, and reports for a model."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, json_col


async def get_model_actions(model_name: str, get_db: Callable[[], Path]) -> dict:
    """Get all actions, menus, cron jobs, and reports associated with an Odoo model."""
    db_path = get_db()

    # Window / server / client actions
    action_rows = await async_query(
        db_path,
        """
        SELECT xml_id, action_type, name, res_model, view_mode,
               domain, context_expr, target, binding_model, binding_views,
               server_method, tag, report_name, report_model,
               module_name, file_path
        FROM actions
        WHERE res_model = ? OR binding_model = ? OR report_model = ?
        ORDER BY action_type, name
        """,
        (model_name, model_name, model_name),
    )

    actions = []
    reports = []
    for row in action_rows:
        entry = {
            "xml_id": row.get("xml_id"),
            "type": row.get("action_type"),
            "name": row.get("name"),
            "module": row.get("module_name"),
            "file": row.get("file_path"),
        }
        if row.get("action_type") == "report":
            entry["report_name"] = row.get("report_name")
            entry["report_model"] = row.get("report_model")
            reports.append(entry)
        else:
            if row.get("view_mode"):
                entry["view_mode"] = row.get("view_mode")
            if row.get("domain"):
                entry["domain"] = row.get("domain")
            if row.get("target"):
                entry["target"] = row.get("target")
            if row.get("server_method"):
                entry["server_method"] = row.get("server_method")
            if row.get("binding_model"):
                entry["binding_model"] = row.get("binding_model")
                entry["binding_views"] = json_col(row, "binding_views", [])
            if row.get("tag"):
                entry["tag"] = row.get("tag")
            actions.append(entry)

    # Menus that point to this model (via action)
    menu_rows = await async_query(
        db_path,
        """
        SELECT xml_id, name, parent_xml_id, sequence,
               action_xml_id, action_type, groups, module_name
        FROM menus
        WHERE res_model = ?
        ORDER BY sequence, name
        """,
        (model_name,),
    )

    menus = [
        {
            "xml_id": r.get("xml_id"),
            "name": r.get("name"),
            "parent": r.get("parent_xml_id"),
            "sequence": r.get("sequence"),
            "action": r.get("action_xml_id"),
            "groups": json_col(r, "groups", []),
            "module": r.get("module_name"),
        }
        for r in menu_rows
    ]

    # Cron jobs
    cron_rows = await async_query(
        db_path,
        """
        SELECT xml_id, name, method_name, method_args,
               interval_number, interval_type, numbercall,
               active, priority, module_name
        FROM cron_jobs
        WHERE model_name = ?
        ORDER BY name
        """,
        (model_name,),
    )

    cron_jobs = [
        {
            "xml_id": r.get("xml_id"),
            "name": r.get("name"),
            "method": r.get("method_name"),
            "args": json_col(r, "method_args", []),
            "interval": f"{r.get('interval_number')} {r.get('interval_type')}",
            "active": bool(r.get("active", 1)),
            "priority": r.get("priority"),
            "module": r.get("module_name"),
        }
        for r in cron_rows
    ]

    # Server actions bound to this model (binding_model)
    server_action_rows = await async_query(
        db_path,
        """
        SELECT xml_id, name, server_method, server_code,
               binding_views, module_name
        FROM actions
        WHERE action_type = 'server' AND binding_model = ?
        ORDER BY name
        """,
        (model_name,),
    )

    server_actions = [
        {
            "xml_id": r.get("xml_id"),
            "name": r.get("name"),
            "method": r.get("server_method"),
            "binding_views": json_col(r, "binding_views", []),
            "module": r.get("module_name"),
        }
        for r in server_action_rows
    ]

    return {
        "model": model_name,
        "actions": actions,
        "server_actions": server_actions,
        "menus": menus,
        "cron_jobs": cron_jobs,
        "reports": reports,
        "totals": {
            "actions": len(actions),
            "server_actions": len(server_actions),
            "menus": len(menus),
            "cron_jobs": len(cron_jobs),
            "reports": len(reports),
        },
    }
