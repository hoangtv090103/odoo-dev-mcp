"""
lxml-based XML parser for Odoo view/data files.

Extracts: views, actions, menus, cron jobs, mail templates,
qweb templates, access rules, record rules, and view element refs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lxml import etree


# ── Fast parser (no network, no DTD) ─────────────────────────────────────────

_FAST_PARSER = etree.XMLParser(
    recover=True,
    no_network=True,
    remove_comments=True,
    resolve_entities=False,
)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ViewRecord:
    xml_id: Optional[str]
    name: Optional[str]
    model: Optional[str]
    view_type: Optional[str]
    inherit_id: Optional[str]
    priority: int
    arch_field_names: list[str]
    arch_button_names: list[str]
    view_group: Optional[str]
    module_name: str
    file_path: str
    elements: list["ViewElementRef"] = field(default_factory=list)


@dataclass
class ViewElementRef:
    view_xml_id: Optional[str]
    element_type: str
    field_name: Optional[str] = None
    widget_name: Optional[str] = None
    field_options: Optional[str] = None
    field_attrs: Optional[str] = None
    button_name: Optional[str] = None
    button_type: str = "object"
    button_action: Optional[str] = None
    button_confirm: Optional[str] = None
    button_states: Optional[str] = None
    button_groups: Optional[str] = None
    xpath_expr: Optional[str] = None
    filter_domain: Optional[str] = None
    filter_fields: list[str] = field(default_factory=list)
    groups_attr: Optional[str] = None
    invisible_expr: Optional[str] = None
    attrs_expr: Optional[str] = None
    parent_element: Optional[str] = None
    depth: int = 0


@dataclass
class ActionRecord:
    xml_id: Optional[str]
    action_type: str
    name: Optional[str]
    res_model: Optional[str]
    view_mode: Optional[str]
    domain: Optional[str]
    context_expr: Optional[str]
    target: Optional[str]
    binding_model: Optional[str]
    binding_views: list[str]
    server_method: Optional[str]
    tag: Optional[str]
    report_name: Optional[str]
    report_model: Optional[str]
    domain_fields: list[str]
    module_name: str
    file_path: str


@dataclass
class MenuRecord:
    xml_id: Optional[str]
    name: str
    parent_xml_id: Optional[str]
    sequence: int
    action_xml_id: Optional[str]
    action_type: Optional[str]
    res_model: Optional[str]
    groups: list[str]
    web_icon: Optional[str]
    module_name: str
    file_path: str


@dataclass
class CronRecord:
    xml_id: Optional[str]
    name: Optional[str]
    model_name: str
    method_name: str
    method_args: Optional[str]
    interval_number: int
    interval_type: str
    numbercall: int
    doall: int
    priority: int
    active: int
    module_name: str
    file_path: str


@dataclass
class EmailTemplateRecord:
    xml_id: Optional[str]
    name: Optional[str]
    model_name: str
    subject: Optional[str]
    body_field_refs: list[str]
    email_from: Optional[str]
    email_to: Optional[str]
    reply_to: Optional[str]
    report_template: Optional[str]
    module_name: str
    file_path: str


@dataclass
class QwebTemplateRecord:
    xml_id: str
    name: Optional[str]
    inherit_id: Optional[str]
    priority: int
    template_type: str
    report_model: Optional[str]
    t_calls: list[str]
    t_fields: list[str]
    t_if_fields: list[str]
    module_name: str
    file_path: str


@dataclass
class AccessRuleRecord:
    xml_id: Optional[str]
    name: Optional[str]
    model_name: str
    group_xml_id: Optional[str]
    perm_read: int
    perm_write: int
    perm_create: int
    perm_unlink: int
    module_name: str
    file_path: str


@dataclass
class RecordRuleRecord:
    xml_id: Optional[str]
    name: Optional[str]
    model_name: str
    domain_force: Optional[str]
    groups: list[str]
    perm_read: int
    perm_write: int
    perm_create: int
    perm_unlink: int
    module_name: str
    file_path: str


@dataclass
class XmlFileResult:
    views: list[ViewRecord] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    menus: list[MenuRecord] = field(default_factory=list)
    cron_jobs: list[CronRecord] = field(default_factory=list)
    email_templates: list[EmailTemplateRecord] = field(default_factory=list)
    qweb_templates: list[QwebTemplateRecord] = field(default_factory=list)
    access_rules: list[AccessRuleRecord] = field(default_factory=list)
    record_rules: list[RecordRuleRecord] = field(default_factory=list)


# ── xml_id helpers ────────────────────────────────────────────────────────────

def build_xml_id(element: etree._Element, module_name: str) -> Optional[str]:
    raw_id = element.get("id")
    if not raw_id:
        return None
    if "." in raw_id:
        return raw_id
    return f"{module_name}.{raw_id}"


def get_field_value(record: etree._Element, field_name: str) -> Optional[str]:
    """Get the text or ref value of a <field name="..."> inside a record."""
    for f in record.findall(f"field[@name='{field_name}']"):
        ref = f.get("ref")
        if ref:
            return ref
        text = (f.text or "").strip()
        return text or None
    return None


def get_field_int(record: etree._Element, field_name: str, default: int = 0) -> int:
    v = get_field_value(record, field_name)
    if v is None:
        return default
    try:
        # Handle True/False
        if v.lower() == "true":
            return 1
        if v.lower() == "false":
            return 0
        return int(v)
    except ValueError:
        return default


# ── Domain field extraction ───────────────────────────────────────────────────

_DOMAIN_FIELD_RE = re.compile(r"""['"]([\w.]+)['"]""")


def extract_domain_fields(domain_expr: Optional[str]) -> list[str]:
    """Extract field names from a domain string like [('name','=','x')]."""
    if not domain_expr:
        return []
    fields = []
    # A domain tuple is ('field', operator, value)
    # We want first element of each tuple
    for m in re.finditer(r"""\(\s*['"]([\w.]+)['"]\s*,""", domain_expr):
        fields.append(m.group(1))
    return list(dict.fromkeys(fields))  # deduplicate, preserve order


# ── Email template field refs ─────────────────────────────────────────────────

_TEMPLATE_REF_RE = re.compile(r"""\$\{(?:object|record)\.([\w.]+)\}""")


def extract_template_field_refs(body: Optional[str]) -> list[str]:
    if not body:
        return []
    return list(dict.fromkeys(_TEMPLATE_REF_RE.findall(body)))


# ── View arch analysis ────────────────────────────────────────────────────────

def analyse_arch(
    arch_el: etree._Element,
    view_xml_id: Optional[str],
) -> tuple[list[str], list[str], Optional[str], list[ViewElementRef]]:
    """Return (field_names, button_names, view_group, view_elements)."""
    field_names: list[str] = []
    button_names: list[str] = []
    elements: list[ViewElementRef] = []
    view_group = None

    # Root element groups
    if len(arch_el):
        root = arch_el[0]
        view_group = root.get("groups")

    for elem in arch_el.iter():
        tag = elem.tag
        if tag == "field":
            fn = elem.get("name")
            if fn and fn not in field_names:
                field_names.append(fn)
            widget = elem.get("widget")
            attrs = elem.get("attrs")
            groups = elem.get("groups")
            invisible = elem.get("invisible")
            options = elem.get("options")
            if widget or attrs or groups or invisible:
                elements.append(ViewElementRef(
                    view_xml_id=view_xml_id,
                    element_type="field",
                    field_name=fn,
                    widget_name=widget,
                    field_options=options,
                    field_attrs=attrs,
                    groups_attr=groups,
                    invisible_expr=invisible or elem.get("column_invisible"),
                    attrs_expr=attrs,
                ))

        elif tag == "button":
            bn = elem.get("name")
            if bn:
                if bn not in button_names:
                    button_names.append(bn)
                elements.append(ViewElementRef(
                    view_xml_id=view_xml_id,
                    element_type="button",
                    button_name=bn,
                    button_type=elem.get("type", "object"),
                    button_action=elem.get("action"),
                    button_confirm=elem.get("confirm"),
                    button_states=elem.get("states"),
                    button_groups=elem.get("groups"),
                    groups_attr=elem.get("groups"),
                    invisible_expr=elem.get("invisible") or elem.get("attrs"),
                ))

        elif tag == "filter":
            domain = elem.get("domain")
            ff = extract_domain_fields(domain)
            elements.append(ViewElementRef(
                view_xml_id=view_xml_id,
                element_type="filter",
                filter_domain=domain,
                filter_fields=ff,
            ))

        elif tag == "xpath":
            elements.append(ViewElementRef(
                view_xml_id=view_xml_id,
                element_type="xpath",
                xpath_expr=elem.get("expr"),
            ))

    return field_names, button_names, view_group, elements


# ── QWeb template analysis ────────────────────────────────────────────────────

def analyse_qweb(template_el: etree._Element) -> tuple[list[str], list[str], list[str]]:
    """Return (t_calls, t_fields, t_if_fields) from a QWeb template."""
    t_calls: list[str] = []
    t_fields: list[str] = []
    t_if_fields: list[str] = []

    for elem in template_el.iter():
        t_call = elem.get("t-call")
        if t_call and t_call not in t_calls:
            t_calls.append(t_call)

        t_field = elem.get("t-field")
        if t_field:
            # e.g. o.name → strip object prefix
            parts = t_field.split(".", 1)
            fn = parts[-1] if len(parts) > 1 else t_field
            if fn and fn not in t_fields:
                t_fields.append(fn)

        t_if = elem.get("t-if")
        if t_if:
            for m in re.finditer(r"""\.(\w+)""", t_if):
                f = m.group(1)
                if f and f not in t_if_fields:
                    t_if_fields.append(f)

    return t_calls, t_fields, t_if_fields


# ── Main parser ───────────────────────────────────────────────────────────────

class XmlFileParser:
    """Parse a single Odoo XML data file and extract all record types."""

    def __init__(self, path: Path, module_name: str):
        self.path = path
        self.module_name = module_name
        self.file_str = str(path)

    def parse(self) -> XmlFileResult:
        result = XmlFileResult()
        try:
            content = self.path.read_bytes()
            tree = etree.fromstring(content, parser=_FAST_PARSER)
        except Exception:
            return result

        # Find root — could be <odoo>, <openerp>, or bare element
        root = tree
        if root.tag in ("odoo", "openerp"):
            data_els = root.findall("data") or [root]
        else:
            data_els = [root]

        for data_el in data_els:
            self._parse_records(data_el, result)
            # Also parse <menuitem> directly under data
            for menu_el in data_el.findall(".//menuitem"):
                m = self._parse_menuitem(menu_el)
                if m:
                    result.menus.append(m)
            # QWeb templates as <template> shorthand
            for tpl_el in data_el.findall(".//template"):
                t = self._parse_qweb_template_shorthand(tpl_el)
                if t:
                    result.qweb_templates.append(t)

        return result

    def _parse_records(self, data_el: etree._Element, result: XmlFileResult) -> None:
        for record in data_el.findall(".//record"):
            model = record.get("model", "")
            if model == "ir.ui.view":
                v = self._parse_view(record)
                if v:
                    result.views.append(v)
            elif model == "ir.actions.act_window":
                a = self._parse_act_window(record)
                if a:
                    result.actions.append(a)
            elif model == "ir.actions.server":
                a = self._parse_server_action(record)
                if a:
                    result.actions.append(a)
            elif model == "ir.actions.client":
                a = self._parse_client_action(record)
                if a:
                    result.actions.append(a)
            elif model in ("ir.actions.report", "ir.actions.report.xml"):
                a = self._parse_report_action(record)
                if a:
                    result.actions.append(a)
            elif model == "ir.ui.menu":
                m = self._parse_menu_record(record)
                if m:
                    result.menus.append(m)
            elif model == "ir.cron":
                c = self._parse_cron(record)
                if c:
                    result.cron_jobs.append(c)
            elif model == "mail.template":
                e = self._parse_email_template(record)
                if e:
                    result.email_templates.append(e)
            elif model == "ir.ui.view" or model == "ir.qweb":
                pass  # handled above
            elif model == "ir.model.access":
                ar = self._parse_access_rule(record)
                if ar:
                    result.access_rules.append(ar)
            elif model == "ir.rule":
                rr = self._parse_record_rule(record)
                if rr:
                    result.record_rules.append(rr)

    # ── View parser ───────────────────────────────────────────────────────────

    def _parse_view(self, record: etree._Element) -> Optional[ViewRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        model = get_field_value(record, "model")
        view_type = get_field_value(record, "type")
        inherit_id_val = None
        inherit_f = record.find("field[@name='inherit_id']")
        if inherit_f is not None:
            inherit_id_val = inherit_f.get("ref") or (inherit_f.text or "").strip() or None
        priority = get_field_int(record, "priority", 16)

        # Get arch
        arch_f = record.find("field[@name='arch']")
        field_names: list[str] = []
        button_names: list[str] = []
        view_group = None
        elements: list[ViewElementRef] = []

        if arch_f is not None:
            field_names, button_names, view_group, elements = analyse_arch(arch_f, xml_id)
            # Only fall back to arch root tag when there is NO inherit_id AND no type field.
            # Inherited views have <xpath> as root — that describes the patch, not the view type.
            if not view_type and not inherit_id_val and len(arch_f):
                arch_root_tag = arch_f[0].tag
                # Only use known Odoo view type tags, not structural tags like 'xpath'
                if arch_root_tag in {
                    "form", "list", "tree", "kanban", "search",
                    "calendar", "gantt", "pivot", "graph", "map",
                    "activity", "cohort", "qweb",
                }:
                    view_type = arch_root_tag

        return ViewRecord(
            xml_id=xml_id,
            name=name,
            model=model,
            view_type=view_type,
            inherit_id=inherit_id_val,
            priority=priority,
            arch_field_names=field_names,
            arch_button_names=button_names,
            view_group=view_group,
            module_name=self.module_name,
            file_path=self.file_str,
            elements=elements,
        )

    # ── Action parsers ────────────────────────────────────────────────────────

    def _parse_act_window(self, record: etree._Element) -> Optional[ActionRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        res_model = get_field_value(record, "res_model")
        view_mode = get_field_value(record, "view_mode")
        domain = get_field_value(record, "domain")
        context_expr = get_field_value(record, "context")
        target = get_field_value(record, "target")
        binding_model = get_field_value(record, "binding_model_id")
        binding_views_raw = get_field_value(record, "binding_view_types")
        binding_views = [v.strip() for v in binding_views_raw.split(",")] if binding_views_raw else []
        domain_fields = extract_domain_fields(domain)
        return ActionRecord(
            xml_id=xml_id, action_type="act_window", name=name,
            res_model=res_model, view_mode=view_mode, domain=domain,
            context_expr=context_expr, target=target,
            binding_model=binding_model, binding_views=binding_views,
            server_method=None, tag=None, report_name=None, report_model=None,
            domain_fields=domain_fields, module_name=self.module_name,
            file_path=self.file_str,
        )

    def _parse_server_action(self, record: etree._Element) -> Optional[ActionRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        binding_model = get_field_value(record, "binding_model_id")
        binding_views_raw = get_field_value(record, "binding_view_types")
        binding_views = [v.strip() for v in binding_views_raw.split(",")] if binding_views_raw else []
        server_method = get_field_value(record, "action_server_id") or get_field_value(record, "method_name")
        return ActionRecord(
            xml_id=xml_id, action_type="server", name=name,
            res_model=get_field_value(record, "model_id"),
            view_mode=None, domain=None, context_expr=None, target=None,
            binding_model=binding_model, binding_views=binding_views,
            server_method=server_method, tag=None, report_name=None, report_model=None,
            domain_fields=[], module_name=self.module_name, file_path=self.file_str,
        )

    def _parse_client_action(self, record: etree._Element) -> Optional[ActionRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        tag = get_field_value(record, "tag")
        return ActionRecord(
            xml_id=xml_id, action_type="client", name=name,
            res_model=None, view_mode=None, domain=None, context_expr=None, target=None,
            binding_model=None, binding_views=[], server_method=None,
            tag=tag, report_name=None, report_model=None,
            domain_fields=[], module_name=self.module_name, file_path=self.file_str,
        )

    def _parse_report_action(self, record: etree._Element) -> Optional[ActionRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        report_name = get_field_value(record, "report_name")
        report_model = get_field_value(record, "model") or get_field_value(record, "binding_model_id")
        return ActionRecord(
            xml_id=xml_id, action_type="report", name=name,
            res_model=report_model, view_mode=None, domain=None, context_expr=None, target=None,
            binding_model=report_model, binding_views=[], server_method=None,
            tag=None, report_name=report_name, report_model=report_model,
            domain_fields=[], module_name=self.module_name, file_path=self.file_str,
        )

    # ── Menu parsers ──────────────────────────────────────────────────────────

    def _parse_menuitem(self, elem: etree._Element) -> Optional[MenuRecord]:
        xml_id = build_xml_id(elem, self.module_name)
        name = elem.get("name", "")
        if not name:
            return None
        parent_id = elem.get("parent")
        if parent_id and "." not in parent_id:
            parent_id = f"{self.module_name}.{parent_id}"
        action = elem.get("action")
        if action and "." not in action:
            action = f"{self.module_name}.{action}"
        groups_raw = elem.get("groups", "")
        groups = [g.strip() for g in groups_raw.split(",") if g.strip()] if groups_raw else []
        sequence = int(elem.get("sequence", "10"))
        return MenuRecord(
            xml_id=xml_id, name=name, parent_xml_id=parent_id,
            sequence=sequence, action_xml_id=action, action_type=None,
            res_model=None, groups=groups, web_icon=elem.get("web_icon"),
            module_name=self.module_name, file_path=self.file_str,
        )

    def _parse_menu_record(self, record: etree._Element) -> Optional[MenuRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name") or ""
        parent_ref = record.find("field[@name='parent_id']")
        parent_xml_id = parent_ref.get("ref") if parent_ref is not None else None
        action_ref = record.find("field[@name='action']")
        action_xml_id = action_ref.get("ref") if action_ref is not None else None
        groups_raw = get_field_value(record, "groups_id") or ""
        groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        sequence = get_field_int(record, "sequence", 10)
        return MenuRecord(
            xml_id=xml_id, name=name, parent_xml_id=parent_xml_id,
            sequence=sequence, action_xml_id=action_xml_id, action_type=None,
            res_model=None, groups=groups, web_icon=None,
            module_name=self.module_name, file_path=self.file_str,
        )

    # ── Cron parser ───────────────────────────────────────────────────────────

    def _parse_cron(self, record: etree._Element) -> Optional[CronRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        model_name = get_field_value(record, "model_id") or get_field_value(record, "model")
        method_name = get_field_value(record, "function") or get_field_value(record, "code") or ""
        if not model_name or not method_name:
            return None
        method_args = get_field_value(record, "args")
        interval_number = get_field_int(record, "interval_number", 1)
        interval_type = get_field_value(record, "interval_type") or "months"
        numbercall = get_field_int(record, "numbercall", -1)
        doall = get_field_int(record, "doall", 0)
        priority = get_field_int(record, "priority", 5)
        active = get_field_int(record, "active", 1)
        return CronRecord(
            xml_id=xml_id, name=name, model_name=model_name,
            method_name=method_name, method_args=method_args,
            interval_number=interval_number, interval_type=interval_type,
            numbercall=numbercall, doall=doall, priority=priority, active=active,
            module_name=self.module_name, file_path=self.file_str,
        )

    # ── Email template parser ─────────────────────────────────────────────────

    def _parse_email_template(self, record: etree._Element) -> Optional[EmailTemplateRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        model_name = get_field_value(record, "model_id") or get_field_value(record, "model")
        if not model_name:
            return None
        subject = get_field_value(record, "subject")
        body_f = record.find("field[@name='body_html']")
        body_html = (body_f.text or "") if body_f is not None else ""
        body_field_refs = extract_template_field_refs(body_html)
        return EmailTemplateRecord(
            xml_id=xml_id, name=name, model_name=model_name, subject=subject,
            body_field_refs=body_field_refs,
            email_from=get_field_value(record, "email_from"),
            email_to=get_field_value(record, "email_to"),
            reply_to=get_field_value(record, "reply_to"),
            report_template=get_field_value(record, "report_template"),
            module_name=self.module_name, file_path=self.file_str,
        )

    # ── QWeb template ─────────────────────────────────────────────────────────

    def _parse_qweb_template_shorthand(self, elem: etree._Element) -> Optional[QwebTemplateRecord]:
        raw_id = elem.get("id") or elem.get("t-name")
        if not raw_id:
            return None
        xml_id = f"{self.module_name}.{raw_id}" if "." not in raw_id else raw_id
        inherit_id = elem.get("inherit_id") or elem.get("t-inherit")
        priority = int(elem.get("priority", "16"))
        t_calls, t_fields, t_if_fields = analyse_qweb(elem)
        template_type = "report" if "report" in raw_id.lower() else "qweb"
        return QwebTemplateRecord(
            xml_id=xml_id, name=elem.get("name"),
            inherit_id=inherit_id, priority=priority,
            template_type=template_type, report_model=None,
            t_calls=t_calls, t_fields=t_fields, t_if_fields=t_if_fields,
            module_name=self.module_name, file_path=self.file_str,
        )

    # ── Access / Record rules ─────────────────────────────────────────────────

    def _parse_access_rule(self, record: etree._Element) -> Optional[AccessRuleRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        model_ref = record.find("field[@name='model_id']")
        model_name = model_ref.get("ref") if model_ref is not None else get_field_value(record, "model_id")
        if not model_name:
            return None
        group_ref = record.find("field[@name='group_id']")
        group_xml_id = group_ref.get("ref") if group_ref is not None else None
        return AccessRuleRecord(
            xml_id=xml_id, name=name, model_name=model_name,
            group_xml_id=group_xml_id,
            perm_read=get_field_int(record, "perm_read", 0),
            perm_write=get_field_int(record, "perm_write", 0),
            perm_create=get_field_int(record, "perm_create", 0),
            perm_unlink=get_field_int(record, "perm_unlink", 0),
            module_name=self.module_name, file_path=self.file_str,
        )

    def _parse_record_rule(self, record: etree._Element) -> Optional[RecordRuleRecord]:
        xml_id = build_xml_id(record, self.module_name)
        name = get_field_value(record, "name")
        model_ref = record.find("field[@name='model_id']")
        model_name = model_ref.get("ref") if model_ref is not None else get_field_value(record, "model_id")
        if not model_name:
            return None
        domain = get_field_value(record, "domain_force")
        groups_raw = get_field_value(record, "groups") or ""
        groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        return RecordRuleRecord(
            xml_id=xml_id, name=name, model_name=model_name,
            domain_force=domain, groups=groups,
            perm_read=get_field_int(record, "perm_read", 1),
            perm_write=get_field_int(record, "perm_write", 1),
            perm_create=get_field_int(record, "perm_create", 1),
            perm_unlink=get_field_int(record, "perm_unlink", 1),
            module_name=self.module_name, file_path=self.file_str,
        )


# ── CSV access rules ──────────────────────────────────────────────────────────

def parse_csv_access(path: Path, module_name: str) -> list[AccessRuleRecord]:
    """Parse ir.model.access.csv file."""
    import csv
    results = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name") or row.get("id", "")
                model_raw = row.get("model_id:id") or row.get("model_id") or row.get("model", "")
                # model_raw may be 'model_sale_order' or 'sale.order'
                model_name = _csv_model_id_to_name(model_raw)
                group_raw = row.get("group_id:id") or row.get("group_id", "")
                group_xml_id = group_raw or None
                results.append(AccessRuleRecord(
                    xml_id=f"{module_name}.{name}" if name else None,
                    name=name, model_name=model_name,
                    group_xml_id=group_xml_id,
                    perm_read=int(row.get("perm_read", 0)),
                    perm_write=int(row.get("perm_write", 0)),
                    perm_create=int(row.get("perm_create", 0)),
                    perm_unlink=int(row.get("perm_unlink", 0)),
                    module_name=module_name, file_path=str(path),
                ))
    except Exception:
        pass
    return results


def _csv_model_id_to_name(model_id: str) -> str:
    """Convert 'model_sale_order' → 'sale.order', or pass through if already dotted."""
    if not model_id:
        return ""
    if "." in model_id:
        # May have 'base.model_sale_order'
        parts = model_id.rsplit(".", 1)
        model_id = parts[-1]
    if model_id.startswith("model_"):
        return model_id[6:].replace("_", ".")
    return model_id


# ── Convenience function ──────────────────────────────────────────────────────

def parse_xml_file(path: Path, module_name: str) -> XmlFileResult:
    """Parse an XML data file and return all extracted records."""
    parser = XmlFileParser(path, module_name)
    return parser.parse()
