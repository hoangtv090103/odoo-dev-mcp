"""
tree-sitter–based Python parser for Odoo source files.

Provides low-level extraction primitives used by the indexer.
Supports tree-sitter >= 0.21 API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── tree-sitter setup ─────────────────────────────────────────────────────────

try:
    from tree_sitter import Language, Parser, Node
    import tree_sitter_python as _tspy

    _PY_LANG = Language(_tspy.language())
    _PARSER = Parser(_PY_LANG)
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    _PY_LANG = None  # type: ignore
    _PARSER = None   # type: ignore
    Node = object    # type: ignore


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ClassInfo:
    name: str
    bases: list[str]
    line_number: int
    body_start: int
    body_end: int
    docstring: Optional[str] = None


@dataclass
class DecoratorInfo:
    name: str           # 'api.depends', 'http.route', etc.
    args: list[str] = field(default_factory=list)
    kwargs: dict[str, str] = field(default_factory=dict)
    raw: str = ""


@dataclass
class MethodInfo:
    name: str
    class_name: Optional[str]
    decorators: list[DecoratorInfo]
    line_number: int
    body_start: int
    body_end: int
    args: list[str] = field(default_factory=list)
    docstring: Optional[str] = None
    body_text: str = ""


@dataclass
class AssignmentInfo:
    name: str           # attribute name
    value_text: str     # raw text of RHS
    line_number: int


@dataclass
class OdooModelInfo:
    class_name: str
    model_name: Optional[str]           # _name
    inherit: Optional[str | list[str]]  # _inherit
    inherits: dict[str, str]            # _inherits
    description: Optional[str]          # _description
    table_name: Optional[str]           # _table
    rec_name: Optional[str]             # _rec_name
    order: Optional[str]                # _order
    is_abstract: bool
    is_transient: bool
    line_number: int
    file_path: str
    methods: list[MethodInfo] = field(default_factory=list)
    fields: list["FieldInfo"] = field(default_factory=list)


@dataclass
class FieldInfo:
    name: str
    field_type: str     # 'Char', 'Many2one', etc.
    kwargs: dict[str, str]
    line_number: int


# ── Parser helpers ────────────────────────────────────────────────────────────

def _parse_source(source: str) -> "Node":
    """Parse Python source text into a tree-sitter tree."""
    if not TREE_SITTER_AVAILABLE or _PARSER is None:
        raise RuntimeError("tree-sitter is not installed. Run: pip install tree-sitter tree-sitter-python")
    tree = _PARSER.parse(bytes(source, "utf-8"))
    return tree.root_node


def _node_text(node: "Node", source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: "Node", field_name: str) -> Optional["Node"]:
    return node.child_by_field_name(field_name)


# ── String literal extraction ─────────────────────────────────────────────────

_STR_LITERAL_RE = re.compile(r"""^[bBrRfFuU]*(?:'([^']*)'|"([^"]*)")$""")


def unquote(text: str) -> Optional[str]:
    """Unquote a Python string literal to its value, or None if not a simple string."""
    text = text.strip()
    m = _STR_LITERAL_RE.match(text)
    if m:
        return m.group(1) if m.group(1) is not None else m.group(2)
    # Triple-quoted
    for q in ('"""', "'''"):
        if text.startswith(q) and text.endswith(q) and len(text) > 6:
            return text[3:-3]
    return None


# ── AST-level traversal (tree-sitter) ────────────────────────────────────────

class PythonFileParser:
    """Parse a single .py file and extract Odoo-relevant constructs."""

    def __init__(self, source: str, file_path: str = ""):
        self.source = source
        self.source_bytes = source.encode("utf-8")
        self.file_path = file_path
        if TREE_SITTER_AVAILABLE:
            self.root = _parse_source(source)
        else:
            self.root = None
        self._classes: list[ClassInfo] = []
        self._methods: list[MethodInfo] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def extract_odoo_models(self) -> list[OdooModelInfo]:
        """Return all Odoo model class definitions in the file."""
        models = []
        classes = self._get_classes()
        for cls in classes:
            info = self._analyse_class(cls)
            if info is not None:
                models.append(info)
        return models

    def extract_http_controllers(self) -> list["ControllerInfo"]:
        """Return Controller classes and their @route methods."""
        controllers = []
        for cls in self._get_classes():
            if self._is_controller(cls):
                methods = self._get_methods_in_class(cls)
                route_methods = [m for m in methods if any(
                    d.name == "http.route" or d.name == "route"
                    for d in m.decorators
                )]
                if route_methods:
                    controllers.append(ControllerInfo(
                        class_name=cls.name,
                        route_methods=route_methods,
                        file_path=self.file_path,
                    ))
        return controllers

    # ── Class discovery ───────────────────────────────────────────────────────

    def _get_classes(self) -> list[ClassInfo]:
        if self.root is None:
            return []
        results = []
        self._walk_classes(self.root, results)
        return results

    def _walk_classes(self, node: "Node", out: list) -> None:
        if node.type == "class_definition":
            name_node = _child_by_field(node, "name")
            bases_node = _child_by_field(node, "superclasses")
            name = _node_text(name_node, self.source_bytes) if name_node else ""
            bases = self._parse_bases(bases_node)
            body_node = _child_by_field(node, "body")
            body_start = body_node.start_point[0] + 1 if body_node else node.start_point[0] + 1
            body_end = body_node.end_point[0] + 1 if body_node else node.end_point[0] + 1
            docstring = self._extract_class_docstring(body_node)
            out.append(ClassInfo(
                name=name,
                bases=bases,
                line_number=node.start_point[0] + 1,
                body_start=body_start,
                body_end=body_end,
                docstring=docstring,
            ))
            # recurse for nested classes
            if body_node:
                for child in body_node.children:
                    self._walk_classes(child, out)
            return
        for child in node.children:
            self._walk_classes(child, out)

    def _parse_bases(self, bases_node: Optional["Node"]) -> list[str]:
        if bases_node is None:
            return []
        return [
            _node_text(child, self.source_bytes).strip()
            for child in bases_node.children
            if child.type not in (",", "(", ")")
        ]

    def _extract_class_docstring(self, body_node: Optional["Node"]) -> Optional[str]:
        if body_node is None:
            return None
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        raw = _node_text(sub, self.source_bytes)
                        return unquote(raw)
        return None

    # ── Method extraction ─────────────────────────────────────────────────────

    def _get_methods_in_class(self, cls: ClassInfo) -> list[MethodInfo]:
        """Extract all methods defined in the body of a class."""
        if self.root is None:
            return []
        methods = []
        # Find the class node by line number
        cls_node = self._find_class_node(cls.line_number)
        if cls_node is None:
            return []
        body_node = _child_by_field(cls_node, "body")
        if body_node is None:
            return []
        for child in body_node.children:
            method = self._parse_decorated_or_def(child, cls.name)
            if method:
                methods.append(method)
        return methods

    def _find_class_node(self, line_no: int) -> Optional["Node"]:
        """Walk the AST to find a class_definition starting at line_no."""
        return self._find_node_at_line(self.root, "class_definition", line_no)

    def _find_node_at_line(self, node: "Node", node_type: str, line_no: int) -> Optional["Node"]:
        if node.type == node_type and node.start_point[0] + 1 == line_no:
            return node
        for child in node.children:
            result = self._find_node_at_line(child, node_type, line_no)
            if result:
                return result
        return None

    def _parse_decorated_or_def(
        self,
        node: "Node",
        class_name: Optional[str] = None,
    ) -> Optional[MethodInfo]:
        decorators = []
        actual_def = None

        if node.type == "decorated_definition":
            for child in node.children:
                if child.type == "decorator":
                    decorators.append(self._parse_decorator(child))
                elif child.type == "function_definition":
                    actual_def = child
        elif node.type == "function_definition":
            actual_def = node

        if actual_def is None:
            return None

        name_node = _child_by_field(actual_def, "name")
        if not name_node:
            return None
        name = _node_text(name_node, self.source_bytes)

        params_node = _child_by_field(actual_def, "parameters")
        args = self._parse_params(params_node)

        body_node = _child_by_field(actual_def, "body")
        docstring = self._extract_class_docstring(body_node)  # same logic
        body_text = _node_text(body_node, self.source_bytes) if body_node else ""
        body_start = body_node.start_point[0] + 1 if body_node else actual_def.start_point[0] + 1
        body_end = body_node.end_point[0] + 1 if body_node else actual_def.end_point[0] + 1

        return MethodInfo(
            name=name,
            class_name=class_name,
            decorators=decorators,
            line_number=node.start_point[0] + 1,
            body_start=body_start,
            body_end=body_end,
            args=args,
            docstring=docstring,
            body_text=body_text,
        )

    def _parse_params(self, params_node: Optional["Node"]) -> list[str]:
        if params_node is None:
            return []
        result = []
        for child in params_node.children:
            if child.type in ("identifier", "typed_parameter", "default_parameter"):
                ident = child if child.type == "identifier" else _child_by_field(child, "name")
                if ident:
                    result.append(_node_text(ident, self.source_bytes))
        return result

    # ── Decorator parsing ─────────────────────────────────────────────────────

    def _parse_decorator(self, dec_node: "Node") -> DecoratorInfo:
        """Parse a decorator node into a DecoratorInfo."""
        # The child after '@' is the actual expression
        expr_children = [c for c in dec_node.children if c.type != "@" and c.type != "newline"]
        if not expr_children:
            return DecoratorInfo(name="unknown", raw=_node_text(dec_node, self.source_bytes))

        expr = expr_children[0]
        raw = _node_text(dec_node, self.source_bytes).lstrip("@").strip()

        if expr.type == "call":
            func_node = _child_by_field(expr, "function")
            args_node = _child_by_field(expr, "arguments")
            name = self._node_to_dotted(func_node)
            args, kwargs = self._parse_call_args(args_node)
            return DecoratorInfo(name=name, args=args, kwargs=kwargs, raw=raw)
        elif expr.type == "attribute":
            name = self._node_to_dotted(expr)
            return DecoratorInfo(name=name, raw=raw)
        elif expr.type == "identifier":
            name = _node_text(expr, self.source_bytes)
            return DecoratorInfo(name=name, raw=raw)
        else:
            return DecoratorInfo(name=raw.split("(")[0], raw=raw)

    def _node_to_dotted(self, node: Optional["Node"]) -> str:
        if node is None:
            return ""
        if node.type == "identifier":
            return _node_text(node, self.source_bytes)
        if node.type == "attribute":
            obj = _child_by_field(node, "object")
            attr = _child_by_field(node, "attribute")
            return f"{self._node_to_dotted(obj)}.{_node_text(attr, self.source_bytes) if attr else ''}"
        return _node_text(node, self.source_bytes)

    def _parse_call_args(
        self, args_node: Optional["Node"]
    ) -> tuple[list[str], dict[str, str]]:
        args: list[str] = []
        kwargs: dict[str, str] = {}
        if args_node is None:
            return args, kwargs
        for child in args_node.children:
            if child.type == "keyword_argument":
                k_node = _child_by_field(child, "name")
                v_node = _child_by_field(child, "value")
                if k_node and v_node:
                    kwargs[_node_text(k_node, self.source_bytes)] = _node_text(v_node, self.source_bytes)
            elif child.type not in (",", "(", ")"):
                text = _node_text(child, self.source_bytes).strip()
                if text:
                    args.append(text)
        return args, kwargs

    # ── Odoo model analysis ───────────────────────────────────────────────────

    def _analyse_class(self, cls: ClassInfo) -> Optional[OdooModelInfo]:
        """Extract Odoo model metadata from a class."""
        # Find class node
        cls_node = self._find_class_node(cls.line_number)
        if cls_node is None:
            return None

        body_node = _child_by_field(cls_node, "body")
        if body_node is None:
            return None

        assignments = self._extract_class_assignments(body_node)

        model_name = self._get_str_assign(assignments, "_name")
        inherit_raw = assignments.get("_inherit")
        inherits_raw = assignments.get("_inherits")
        description = self._get_str_assign(assignments, "_description")
        table_name = self._get_str_assign(assignments, "_table")
        rec_name = self._get_str_assign(assignments, "_rec_name")
        order = self._get_str_assign(assignments, "_order")

        # Detect abstract/transient
        base_names = [b.lower() for b in cls.bases]
        is_abstract = any(
            "abstractmodel" in b or "abstract" in b
            for b in base_names
        )
        is_transient = any(
            "transientmodel" in b or "transient" in b
            for b in base_names
        )

        # Parse _inherit
        inherit: Optional[str | list[str]] = None
        if inherit_raw:
            v = unquote(inherit_raw.value_text)
            if v:
                inherit = v
            elif inherit_raw.value_text.startswith("["):
                inherit = self._parse_list_literal(inherit_raw.value_text)
            else:
                inherit = inherit_raw.value_text

        # If no _name but has _inherit (string), that IS the model name
        if model_name is None and isinstance(inherit, str):
            model_name = inherit

        # Only include if it looks like an Odoo model
        if not model_name and not inherit:
            if not any("Model" in b or "model" in b for b in cls.bases):
                return None

        # Parse _inherits
        inherits: dict[str, str] = {}
        if inherits_raw:
            inherits = self._parse_dict_literal(inherits_raw.value_text)

        # Extract methods
        methods = self._get_methods_in_class(cls)

        # Extract field definitions
        fields = self._extract_field_defs(body_node)

        return OdooModelInfo(
            class_name=cls.name,
            model_name=model_name,
            inherit=inherit,
            inherits=inherits,
            description=description,
            table_name=table_name,
            rec_name=rec_name,
            order=order,
            is_abstract=is_abstract,
            is_transient=is_transient,
            line_number=cls.line_number,
            file_path=self.file_path,
            methods=methods,
            fields=fields,
        )

    def _extract_class_assignments(self, body_node: "Node") -> dict[str, AssignmentInfo]:
        """Extract simple name = value assignments from a class body."""
        result: dict[str, AssignmentInfo] = {}
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "assignment":
                        left = _child_by_field(sub, "left")
                        right = _child_by_field(sub, "right")
                        if left and right and left.type == "identifier":
                            name = _node_text(left, self.source_bytes)
                            value = _node_text(right, self.source_bytes)
                            result[name] = AssignmentInfo(
                                name=name,
                                value_text=value,
                                line_number=child.start_point[0] + 1,
                            )
        return result

    def _get_str_assign(
        self,
        assignments: dict[str, AssignmentInfo],
        key: str,
    ) -> Optional[str]:
        a = assignments.get(key)
        if a is None:
            return None
        return unquote(a.value_text)

    def _extract_field_defs(self, body_node: "Node") -> list[FieldInfo]:
        """Extract fields.Xxx(...) assignments from a class body."""
        fields = []
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type == "assignment":
                        left = _child_by_field(sub, "left")
                        right = _child_by_field(sub, "right")
                        if left and right and left.type == "identifier":
                            field_name = _node_text(left, self.source_bytes)
                            field_info = self._parse_field_call(field_name, right)
                            if field_info:
                                fields.append(field_info)
        return fields

    def _parse_field_call(self, name: str, node: "Node") -> Optional[FieldInfo]:
        """Try to parse fields.Xxx(...) call into FieldInfo."""
        if node.type != "call":
            return None
        func = _child_by_field(node, "function")
        if func is None:
            return None
        func_text = _node_text(func, self.source_bytes)
        # Must be fields.Xxx
        if not (func_text.startswith("fields.") or func_text in _FIELD_TYPES):
            return None
        field_type = func_text.split(".")[-1]
        if field_type not in _FIELD_TYPES:
            return None
        args_node = _child_by_field(node, "arguments")
        _, kwargs = self._parse_call_args(args_node)
        return FieldInfo(
            name=name,
            field_type=field_type,
            kwargs=kwargs,
            line_number=node.start_point[0] + 1,
        )

    # ── Controller detection ──────────────────────────────────────────────────

    def _is_controller(self, cls: ClassInfo) -> bool:
        return any(
            "Controller" in b or "controller" in b
            for b in cls.bases
        )

    # ── Utility: parse simple Python list/dict literals ───────────────────────

    def _parse_list_literal(self, text: str) -> list[str]:
        """Parse a simple list literal like ['a', 'b'] into Python list."""
        items = re.findall(r"""['"](.*?)['"]""", text)
        return items

    def _parse_dict_literal(self, text: str) -> dict[str, str]:
        """Parse a simple dict literal like {'a': 'b'} into Python dict."""
        result: dict[str, str] = {}
        pairs = re.findall(r"""['"](.*?)['"]\s*:\s*['"](.*?)['"]""", text)
        for k, v in pairs:
            result[k] = v
        return result


# ── Controller info ───────────────────────────────────────────────────────────

@dataclass
class ControllerInfo:
    class_name: str
    route_methods: list[MethodInfo]
    file_path: str


# ── Constants ─────────────────────────────────────────────────────────────────

_FIELD_TYPES = {
    "Boolean", "Integer", "Float", "Monetary",
    "Char", "Text", "Html",
    "Date", "Datetime",
    "Binary", "Image",
    "Selection",
    "Reference",
    "Many2one", "One2many", "Many2many",
    "Many2oneReference",
    "Id",
    "Properties", "PropertiesDefinition",
}


# ── Convenience function ──────────────────────────────────────────────────────

def parse_python_file(path: Path) -> tuple[list[OdooModelInfo], list[ControllerInfo]]:
    """Parse a .py file and return (models, controllers)."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    parser = PythonFileParser(source, str(path))
    models = parser.extract_odoo_models()
    controllers = parser.extract_http_controllers()
    return models, controllers
