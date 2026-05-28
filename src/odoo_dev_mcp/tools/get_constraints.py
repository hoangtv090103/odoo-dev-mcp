"""Tool 06: get_constraints — Python constrains, SQL constraints, and onchange validations."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..db.connection import async_query, json_col


async def get_constraints(model_name: str, get_db: Callable[[], Path]) -> dict:
    """Get all validation constraints for a model: Python @api.constrains,
    SQL constraints, and onchange validations."""
    db_path = get_db()

    # @api.constrains
    constrains_rows = await async_query(
        db_path,
        """
        SELECT dd.method_name, dd.constrains_fields, dd.file_path, dd.line_number
        FROM decorators_detail dd
        WHERE dd.decorator_type = 'api.constrains'
          AND dd.model_name = ?
        ORDER BY dd.method_name
        """,
        (model_name,),
    )

    python_constrains = []
    for row in constrains_rows:
        python_constrains.append(
            {
                "method": row.get("method_name"),
                "constrains_fields": json_col(row, "constrains_fields", []),
                "file": row.get("file_path"),
                "line": row.get("line_number"),
            }
        )

    # @api.onchange
    onchange_rows = await async_query(
        db_path,
        """
        SELECT dd.method_name, dd.onchange_fields, dd.file_path, dd.line_number
        FROM decorators_detail dd
        WHERE dd.decorator_type = 'api.onchange'
          AND dd.model_name = ?
        ORDER BY dd.method_name
        """,
        (model_name,),
    )

    onchange_methods = []
    for row in onchange_rows:
        onchange_methods.append(
            {
                "method": row.get("method_name"),
                "triggers_on": json_col(row, "onchange_fields", []),
                "file": row.get("file_path"),
                "line": row.get("line_number"),
            }
        )

    # Methods that raise ValidationError (raises_validation flag)
    validation_methods = await async_query(
        db_path,
        """
        SELECT method_name, file_path, line_number
        FROM methods
        WHERE model_name = ? AND raises_validation = 1
        ORDER BY method_name
        """,
        (model_name,),
    )

    # Check if model exists at all
    if not python_constrains and not onchange_methods and not validation_methods:
        model_exists = await async_query(
            db_path,
            "SELECT 1 FROM models WHERE name = ? LIMIT 1",
            (model_name,),
        )
        if not model_exists:
            return {
                "error": f"Model '{model_name}' not found in the index.",
                "model": model_name,
            }

    total = len(python_constrains) + len(onchange_methods)

    return {
        "model": model_name,
        "python_constrains": python_constrains,
        "onchange_methods": onchange_methods,
        "validation_methods": [
            {
                "method": r["method_name"],
                "file": r["file_path"],
                "line": r["line_number"],
            }
            for r in validation_methods
        ],
        "total": total,
    }
