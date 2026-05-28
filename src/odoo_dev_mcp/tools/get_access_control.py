"""Tool 07: get_access_control — ACLs, record rules, and field-level group restrictions."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, json_col


async def get_access_control(model_name: str, get_db: Callable[[], Path]) -> dict:
    """Get complete access control for a model: model-level ACLs, record rules,
    and field-level group restrictions."""
    db_path = get_db()

    # Model-level access rules
    acl_rows = await async_query(
        db_path,
        """
        SELECT xml_id, name, group_xml_id,
               perm_read, perm_write, perm_create, perm_unlink,
               module_name
        FROM access_rules
        WHERE model_name = ?
        ORDER BY name
        """,
        (model_name,),
    )

    model_access = []
    for row in acl_rows:
        model_access.append(
            {
                "name": row.get("name"),
                "xml_id": row.get("xml_id"),
                "group": row.get("group_xml_id"),
                "read": bool(row.get("perm_read", 0)),
                "write": bool(row.get("perm_write", 0)),
                "create": bool(row.get("perm_create", 0)),
                "unlink": bool(row.get("perm_unlink", 0)),
                "module": row.get("module_name"),
            }
        )

    # Record rules
    rr_rows = await async_query(
        db_path,
        """
        SELECT xml_id, name, domain_force, groups,
               perm_read, perm_write, perm_create, perm_unlink,
               module_name
        FROM record_rules
        WHERE model_name = ?
        ORDER BY name
        """,
        (model_name,),
    )

    record_rules = []
    for row in rr_rows:
        record_rules.append(
            {
                "name": row.get("name"),
                "xml_id": row.get("xml_id"),
                "domain": row.get("domain_force"),
                "groups": json_col(row, "groups", []),
                "read": bool(row.get("perm_read", 1)),
                "write": bool(row.get("perm_write", 1)),
                "create": bool(row.get("perm_create", 1)),
                "unlink": bool(row.get("perm_unlink", 1)),
                "module": row.get("module_name"),
            }
        )

    # Field-level group restrictions from field_groups_map
    fg_rows = await async_query(
        db_path,
        """
        SELECT field_name, group_xml_id, source, module_name
        FROM field_groups_map
        WHERE model_name = ?
        ORDER BY field_name
        """,
        (model_name,),
    )

    # Also from fields.groups column
    field_groups_from_fields = await async_query(
        db_path,
        """
        SELECT field_name, groups, module_name
        FROM fields
        WHERE model_name = ? AND groups IS NOT NULL AND groups != ''
        ORDER BY field_name
        """,
        (model_name,),
    )

    field_groups: list[dict] = []
    seen: set[tuple] = set()

    for row in fg_rows:
        key = (row["field_name"], row["group_xml_id"])
        if key not in seen:
            seen.add(key)
            field_groups.append(
                {
                    "field": row["field_name"],
                    "group": row["group_xml_id"],
                    "source": row.get("source", "field_groups_map"),
                }
            )

    for row in field_groups_from_fields:
        # groups can be comma-separated
        raw_groups = (row.get("groups") or "").strip()
        for g in raw_groups.split(","):
            g = g.strip()
            if not g:
                continue
            key = (row["field_name"], g)
            if key not in seen:
                seen.add(key)
                field_groups.append(
                    {
                        "field": row["field_name"],
                        "group": g,
                        "source": "field_def",
                    }
                )

    summary = (
        f"{len(model_access)} ACL rule{'s' if len(model_access) != 1 else ''}, "
        f"{len(record_rules)} record rule{'s' if len(record_rules) != 1 else ''}, "
        f"{len(field_groups)} field-level restriction{'s' if len(field_groups) != 1 else ''}"
    )

    return {
        "model": model_name,
        "model_access": model_access,
        "record_rules": record_rules,
        "field_groups": field_groups,
        "summary": summary,
    }
