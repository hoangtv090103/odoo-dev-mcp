"""
Phase 3: Method body analysis.

Uses regex on body_text (already extracted by python_parser) to detect:
  - State transitions: self.write({'state': 'done'}) or record.state = 'value'
  - ORM calls to other models: self.env['other.model']
  - ValidationError raises
  - Builds/updates state_machines table
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from ..parsers.python_parser import OdooModelInfo, parse_python_file, unquote
from .module_scanner import ModuleRecord

logger = logging.getLogger(__name__)


# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches: {'state': 'value'} or {"state": "value"} in self.write({...})
_STATE_WRITE_RE = re.compile(
    r"""['"](state)['"]\s*:\s*['"](\w+)['"]"""
)

# Matches: self.state = 'value' or record.state = 'value'
_STATE_ASSIGN_RE = re.compile(
    r"""(?:self|record|rec|order|invoice|task|[\w]+)\s*\.\s*state\s*=\s*['"](\w+)['"]"""
)

# Matches: self.env['other.model'] or self.env["other.model"]
_ENV_MODEL_RE = re.compile(
    r"""self\s*\.\s*env\s*\[\s*['"]([a-z][a-z0-9._]*)['"]"""
)

# Matches: raise ValidationError(...) or raises ValidationError
_VALIDATION_ERROR_RE = re.compile(
    r"""\braise\s+(?:\w+\.)*ValidationError\b"""
)

# Matches: self.write({'field': 'value'}) — any field write
_FIELD_WRITE_RE = re.compile(
    r"""\.write\s*\(\s*\{[^}]*?['"]([\w]+)['"]\s*:\s*['"](\w+)['"]"""
)

# Matches field assignment that could be a state-like field
_GENERIC_STATE_ASSIGN_RE = re.compile(
    r"""(?:self|record|rec|[\w]+)\s*\.\s*(state|status|stage_id|kanban_state)\s*=\s*['"](\w+)['"]"""
)


# ── Analysis ──────────────────────────────────────────────────────────────────

def _analyse_body(
    body_text: str,
) -> tuple[list[tuple[Optional[str], str]], list[str], int]:
    """
    Analyse a method body for:
      - state transitions: list of (from_state or None, to_state)
      - called models: list of model names from self.env['model']
      - raises_validation: 0 or 1

    Returns: (transitions, called_models, raises_validation)
    """
    transitions: list[tuple[Optional[str], str]] = []
    called_models: list[str] = []

    # State transitions via self.write({'state': 'value'})
    for m in _STATE_WRITE_RE.finditer(body_text):
        to_state = m.group(2)
        transitions.append((None, to_state))

    # State transitions via assignment: .state = 'value'
    for m in _STATE_ASSIGN_RE.finditer(body_text):
        to_state = m.group(1)
        transitions.append((None, to_state))

    # Generic state-like field assignments
    for m in _GENERIC_STATE_ASSIGN_RE.finditer(body_text):
        to_state = m.group(2)
        transitions.append((None, to_state))

    # ORM calls to other models
    seen_models: set[str] = set()
    for m in _ENV_MODEL_RE.finditer(body_text):
        model_name = m.group(1)
        if model_name not in seen_models:
            seen_models.add(model_name)
            called_models.append(model_name)

    # ValidationError raises
    raises_validation = 1 if _VALIDATION_ERROR_RE.search(body_text) else 0

    return transitions, called_models, raises_validation


# ── DB helpers ────────────────────────────────────────────────────────────────

def _update_method_body(
    conn: sqlite3.Connection,
    model_name: str,
    method_name: str,
    module_name: str,
    calls_models: list[str],
    state_transitions: list[tuple[Optional[str], str]],
    raises_validation: int,
) -> None:
    # Build state_transitions JSON: [{from: null, to: 'value'}, ...]
    transitions_json = json.dumps([
        {"from": fr, "to": to}
        for fr, to in state_transitions
    ])

    conn.execute(
        """
        UPDATE methods
        SET calls_models = ?,
            state_transitions = ?,
            raises_validation = ?
        WHERE model_name = ? AND method_name = ? AND module_name = ?
        """,
        (
            json.dumps(calls_models),
            transitions_json,
            raises_validation,
            model_name,
            method_name,
            module_name,
        ),
    )


def _upsert_state_machine(
    conn: sqlite3.Connection,
    model_name: str,
    field_name: str,
    states: list[str],
    transitions: list[dict],
    module_name: str,
) -> None:
    """Insert or merge state machine data."""
    # Check existing
    row = conn.execute(
        "SELECT id, states, transitions FROM state_machines WHERE model_name = ? AND field_name = ?",
        (model_name, field_name),
    ).fetchone()

    if row:
        existing_states = set(json.loads(row[1] if isinstance(row[1], str) else "[]"))
        existing_transitions = json.loads(row[2] if isinstance(row[2], str) else "[]")

        merged_states = list(existing_states | set(states))
        # Merge transitions (deduplicate by from+to+method)
        seen = {(t.get("from"), t.get("to"), t.get("method")) for t in existing_transitions}
        for t in transitions:
            key = (t.get("from"), t.get("to"), t.get("method"))
            if key not in seen:
                existing_transitions.append(t)
                seen.add(key)

        conn.execute(
            """
            UPDATE state_machines
            SET states = ?, transitions = ?
            WHERE model_name = ? AND field_name = ?
            """,
            (
                json.dumps(merged_states),
                json.dumps(existing_transitions),
                model_name,
                field_name,
            ),
        )
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO state_machines
                (model_name, field_name, states, transitions, module_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                model_name,
                field_name,
                json.dumps(states),
                json.dumps(transitions),
                module_name,
            ),
        )


# ── Entry point ────────────────────────────────────────────────────

def run_behavioral(conn: sqlite3.Connection, modules: list[ModuleRecord]) -> None:
    """
    Phase 3: Method body analysis.

    For each Python file in each module:
      - Detect state transitions and ORM calls in method bodies
      - Update methods table
      - Aggregate state transitions into state_machines table
    """
    logger.info("Phase 3: behavioral analysis of %d modules", len(modules))

    # model_name -> field_name -> set of to_states, transitions list
    state_data: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "states": set(),
        "transitions": [],
        "module_name": "",
    }))

    for module in modules:
        for py_file in module.python_files:
            try:
                _process_file(conn, py_file, module.name, state_data)
            except Exception as exc:
                logger.warning("Phase3: error in %s: %s", py_file, exc)

    conn.commit()

    # Build state machines
    for model_name, field_map in state_data.items():
        for field_name, data in field_map.items():
            states = sorted(data["states"])
            transitions = data["transitions"]
            if not states:
                continue
            try:
                _upsert_state_machine(
                    conn,
                    model_name,
                    field_name,
                    states,
                    transitions,
                    data["module_name"],
                )
            except Exception as exc:
                logger.debug(
                    "Phase3: state_machine upsert error %s.%s: %s",
                    model_name, field_name, exc,
                )

    conn.commit()
    logger.info("Phase 3: complete")


def _process_file(
    conn: sqlite3.Connection,
    py_file: Path,
    module_name: str,
    state_data: dict,
) -> None:
    """Process one Python file for Phase 3 data."""
    models, _ = parse_python_file(py_file)

    for model in models:
        effective_name = model.model_name
        if not effective_name and isinstance(model.inherit, str):
            effective_name = model.inherit
        elif not effective_name and isinstance(model.inherit, list) and model.inherit:
            effective_name = model.inherit[0]
        if not effective_name:
            continue

        for method in model.methods:
            if not method.body_text:
                continue

            try:
                transitions, called_models, raises_validation = _analyse_body(
                    method.body_text
                )
            except Exception as exc:
                logger.debug(
                    "Phase3: body analysis error %s.%s: %s",
                    effective_name, method.name, exc,
                )
                continue

            # Update methods table
            try:
                _update_method_body(
                    conn,
                    effective_name,
                    method.name,
                    module_name,
                    called_models,
                    transitions,
                    raises_validation,
                )
            except Exception as exc:
                logger.debug(
                    "Phase3: update_method error %s.%s: %s",
                    effective_name, method.name, exc,
                )

            # Accumulate state machine data
            if transitions:
                sm = state_data[effective_name]["state"]
                sm["module_name"] = module_name
                for from_state, to_state in transitions:
                    sm["states"].add(to_state)
                    if from_state:
                        sm["states"].add(from_state)
                    sm["transitions"].append({
                        "from": from_state,
                        "to": to_state,
                        "method": method.name,
                    })
