"""
Regex-based JavaScript parser for Odoo OWL/widget source files.

No AST — uses targeted regexes to extract:
  - Widget/field registrations: registry.category('fields').add(...)
  - View registrations: registry.category('views').add(...)
  - Action handler registrations: registry.category('actions').add(...)
  - RPC calls with string literal routes/models/methods
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class JsComponent:
    component_type: str          # 'field_widget', 'view_widget', 'action_handler', 'rpc_call'
    widget_name: Optional[str] = None
    handled_types: list[str] = field(default_factory=list)
    component_class: Optional[str] = None
    action_tag: Optional[str] = None
    target_model: Optional[str] = None
    target_method: Optional[str] = None
    target_route: Optional[str] = None
    module_name: str = ""
    file_path: str = ""
    line_number: int = 0


# ── Regex patterns ────────────────────────────────────────────────────────────

# registry.category('fields').add('widget_name', WidgetClass)
# registry.category('fields').add('widget_name', WidgetClass, { ... })
_REGISTRY_ADD_RE = re.compile(
    r"""registry\s*\.\s*category\s*\(\s*['"]([^'"]+)['"]\s*\)\s*\.\s*add\s*\("""
    r"""\s*['"]([^'"]+)['"]\s*(?:,\s*([A-Za-z_$][\w$]*))?""",
    re.MULTILINE,
)

# JSON-RPC / ORM RPC patterns:
#   this.orm.call('model.name', 'method_name', ...)
#   await this.orm.call("model", "method", ...)
_ORM_CALL_RE = re.compile(
    r"""(?:this\s*\.\s*)?orm\s*\.\s*(?:call|read|write|create|unlink|searchRead|search)\s*\("""
    r"""\s*['"]([a-z][a-z0-9._]*)['"](?:\s*,\s*['"](\w+)['"])?""",
    re.MULTILINE,
)

# jsonrpc('/route', ...) or rpc('/route', ...)
_RPC_ROUTE_RE = re.compile(
    r"""(?:jsonrpc|rpc|fetch)\s*\(\s*['"](/[^'"]*)['"]\s*(?:,\s*['"]([^'"]*)['"]\s*(?:,\s*['"]([^'"]*)['"])?)?""",
    re.MULTILINE,
)

# this._rpc({ route: '/path', method: 'model', ...})  (legacy Odoo 14-)
_RPC_DICT_RE = re.compile(
    r"""_rpc\s*\(\s*\{[^}]*?route\s*:\s*['"](/[^'"]+)['""][^}]*?\}""",
    re.MULTILINE | re.DOTALL,
)

# Extract line numbers helper
def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


# ── CATEGORY → component_type mapping ────────────────────────────────────────

_CATEGORY_TYPE: dict[str, str] = {
    "fields": "field_widget",
    "views": "view_widget",
    "actions": "action_handler",
    "systray": "systray_widget",
    "error_dialogs": "error_dialog",
    "services": "service",
}


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_js_file(path: Path, module_name: str) -> list[JsComponent]:
    """
    Parse a JavaScript file and extract Odoo registry registrations and RPC calls.

    Returns a list of JsComponent instances.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    file_str = str(path)
    results: list[JsComponent] = []

    # ── Registry registrations ─────────────────────────────────────────────
    for m in _REGISTRY_ADD_RE.finditer(text):
        category = m.group(1).strip()
        widget_name = m.group(2).strip()
        component_class = (m.group(3) or "").strip() or None
        line_no = _line_of(text, m.start())

        component_type = _CATEGORY_TYPE.get(category, f"{category}_registration")

        action_tag: Optional[str] = None
        if component_type == "action_handler":
            action_tag = widget_name

        results.append(JsComponent(
            component_type=component_type,
            widget_name=widget_name if component_type != "action_handler" else None,
            component_class=component_class,
            action_tag=action_tag,
            module_name=module_name,
            file_path=file_str,
            line_number=line_no,
        ))

    # ── ORM calls ─────────────────────────────────────────────────────────
    for m in _ORM_CALL_RE.finditer(text):
        target_model = m.group(1)
        target_method = m.group(2) or None
        line_no = _line_of(text, m.start())

        results.append(JsComponent(
            component_type="rpc_call",
            target_model=target_model,
            target_method=target_method,
            module_name=module_name,
            file_path=file_str,
            line_number=line_no,
        ))

    # ── Route-based RPC calls ──────────────────────────────────────────────
    for m in _RPC_ROUTE_RE.finditer(text):
        target_route = m.group(1)
        target_model = m.group(2) or None
        target_method = m.group(3) or None
        line_no = _line_of(text, m.start())

        results.append(JsComponent(
            component_type="rpc_call",
            target_route=target_route,
            target_model=target_model,
            target_method=target_method,
            module_name=module_name,
            file_path=file_str,
            line_number=line_no,
        ))

    # ── Legacy _rpc({route: ...}) calls ───────────────────────────────────
    for m in _RPC_DICT_RE.finditer(text):
        target_route = m.group(1)
        line_no = _line_of(text, m.start())

        results.append(JsComponent(
            component_type="rpc_call",
            target_route=target_route,
            module_name=module_name,
            file_path=file_str,
            line_number=line_no,
        ))

    return results
