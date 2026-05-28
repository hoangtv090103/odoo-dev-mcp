"""Tool 03: analyze_change_impact — blast radius analysis for field/method changes."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..db.connection import AsyncConn, json_col


async def analyze_change_impact(
    model_name: str,
    get_db: Callable[[], Path],
    field_name: Optional[str] = None,
    method_name: Optional[str] = None,
) -> dict:
    """Analyze the blast radius of changing a field or method: what views,
    methods, and other models depend on it.

    All queries share a single database connection, eliminating repeated
    open/close overhead (~6 ms × 9 queries = ~54 ms saved on typical hardware).
    """
    db_path = get_db()

    if not field_name and not method_name:
        return {
            "error": "Provide at least one of 'field_name' or 'method_name'.",
            "model": model_name,
        }

    target: dict = {"model": model_name}
    if field_name:
        target["field"] = field_name
    if method_name:
        target["method"] = method_name

    impact: dict = {}
    impact_score = 0

    async with AsyncConn(db_path) as conn:

        # ── Compute dependents (@api.depends) ─────────────────────────────────
        if field_name:
            compute_dep_rows = await conn.query(
                """
                SELECT dd.method_name, dd.depends_fields
                FROM decorators_detail dd
                WHERE dd.decorator_type = 'api.depends'
                  AND dd.model_name = ?
                  AND dd.depends_fields LIKE ?
                """,
                (model_name, f"%{field_name}%"),
            )
            compute_dependents = []
            for row in compute_dep_rows:
                deps = json_col(row, "depends_fields", [])
                if any(field_name in d for d in deps):
                    compute_dependents.append(
                        f"{row['method_name']} depends on {field_name} via @api.depends"
                    )
            impact["compute_dependents"] = compute_dependents
            impact_score += len(compute_dependents)

        # ── View references ────────────────────────────────────────────────────
        if field_name:
            # One query: combines explicit field refs + field_names JSON column
            view_ref_rows = await conn.query(
                """
                SELECT DISTINCT v.xml_id, v.name
                FROM views v
                WHERE v.model = ?
                  AND (
                      v.xml_id IN (
                          SELECT ver.view_xml_id
                          FROM view_element_refs ver
                          WHERE ver.field_name = ?
                      )
                      OR v.field_names LIKE ?
                  )
                """,
                (model_name, field_name, f"%{field_name}%"),
            )
            impact["view_refs"] = [
                f"{r['xml_id']} references field {field_name}"
                for r in view_ref_rows
            ]
            impact_score += len(view_ref_rows)

        elif method_name:
            view_btn_rows = await conn.query(
                """
                SELECT ver.view_xml_id
                FROM view_element_refs ver
                WHERE ver.button_name = ? OR ver.button_action = ?
                """,
                (method_name, method_name),
            )
            impact["view_refs"] = [
                f"{r['view_xml_id']} references button/action {method_name}"
                for r in view_btn_rows
            ]
            impact_score += len(view_btn_rows)

        # ── Button visibility conditions ───────────────────────────────────────
        if field_name:
            btn_vis_rows = await conn.query(
                """
                SELECT ver.button_name, ver.button_states, ver.attrs_expr,
                       ver.invisible_expr, ver.view_xml_id
                FROM view_element_refs ver
                WHERE ver.element_type = 'button'
                  AND (ver.button_states LIKE ? OR ver.attrs_expr LIKE ?
                       OR ver.invisible_expr LIKE ?)
                """,
                (f"%{field_name}%", f"%{field_name}%", f"%{field_name}%"),
            )
            button_visibility = []
            for row in btn_vis_rows:
                desc = f"{row['button_name']} visibility controlled by {field_name}"
                states = row.get("button_states") or ""
                if states:
                    desc += f" (states: {states[:80]})"
                button_visibility.append(desc)
            impact["button_visibility"] = button_visibility
            impact_score += len(button_visibility)

        # ── Onchange triggers ──────────────────────────────────────────────────
        if field_name:
            onchange_rows = await conn.query(
                """
                SELECT dd.method_name, dd.onchange_fields
                FROM decorators_detail dd
                WHERE dd.decorator_type = 'api.onchange'
                  AND dd.model_name = ?
                  AND dd.onchange_fields LIKE ?
                """,
                (model_name, f"%{field_name}%"),
            )
            onchange_triggers = [
                f"{row['method_name']} triggers on {field_name}"
                for row in onchange_rows
                if field_name in json_col(row, "onchange_fields", [])
            ]
            impact["onchange_triggers"] = onchange_triggers
            impact_score += len(onchange_triggers)

        # ── Constraints (@api.constrains) ──────────────────────────────────────
        if field_name:
            constrain_rows = await conn.query(
                """
                SELECT dd.method_name, dd.constrains_fields
                FROM decorators_detail dd
                WHERE dd.decorator_type = 'api.constrains'
                  AND dd.model_name = ?
                  AND dd.constrains_fields LIKE ?
                """,
                (model_name, f"%{field_name}%"),
            )
            constrained_by = [
                f"{row['method_name']} constrains {field_name}"
                for row in constrain_rows
                if field_name in json_col(row, "constrains_fields", [])
            ]
            impact["constrained_by"] = constrained_by
            impact_score += len(constrained_by)

        # ── State machine ──────────────────────────────────────────────────────
        if field_name:
            sm_rows = await conn.query(
                "SELECT * FROM state_machines WHERE model_name = ? AND field_name = ?",
                (model_name, field_name),
            )
            if sm_rows:
                transitions = json_col(sm_rows[0], "transitions", [])
                impact["state_machine"] = (
                    f"{field_name} drives workflow with {len(transitions)} transitions"
                )
                impact_score += 3
            else:
                impact["state_machine"] = None

        # ── Record rules ───────────────────────────────────────────────────────
        if field_name:
            rr_rows = await conn.query(
                "SELECT name, domain_force FROM record_rules "
                "WHERE model_name = ? AND domain_force LIKE ?",
                (model_name, f"%{field_name}%"),
            )
            impact["record_rules"] = [
                f"{r['name']} domain references {field_name}" for r in rr_rows
            ]
            impact_score += len(rr_rows)

        # ── Email template references ──────────────────────────────────────────
        if field_name:
            et_rows = await conn.query(
                """
                SELECT xml_id, name FROM email_templates
                WHERE model_name = ? AND body_field_refs LIKE ?
                """,
                (model_name, f"%{field_name}%"),
            )
            impact["email_template_refs"] = [
                f"{r['xml_id']} ({r['name']}) references {field_name}" for r in et_rows
            ]
            impact_score += len(et_rows)

    # ── Risk level ─────────────────────────────────────────────────────────────
    if impact_score >= 8:
        risk_level = "HIGH"
    elif impact_score >= 3:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    target_label = field_name or method_name
    counts = []
    if impact.get("view_refs"):
        counts.append(f"{len(impact['view_refs'])} views")
    if impact.get("compute_dependents"):
        counts.append(f"{len(impact['compute_dependents'])} compute methods")
    if impact.get("record_rules"):
        counts.append(f"{len(impact['record_rules'])} record rules")
    if impact.get("onchange_triggers"):
        counts.append(f"{len(impact['onchange_triggers'])} onchange methods")
    if impact.get("constrained_by"):
        counts.append(f"{len(impact['constrained_by'])} constraints")

    summary = (
        f"Changing '{target_label}' will impact "
        + (", ".join(counts) if counts else "nothing indexed")
        + "."
    )

    return {
        "target": target,
        "impact": impact,
        "risk_level": risk_level,
        "summary": summary,
    }
