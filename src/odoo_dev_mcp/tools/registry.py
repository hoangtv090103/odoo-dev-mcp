"""MCP tool registry — registers all 19 tools onto a FastMCP instance."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from fastmcp import FastMCP

from .get_model_schema import get_model_schema as _get_model_schema
from .resolve_xml_view import resolve_xml_view as _resolve_xml_view
from .analyze_change_impact import analyze_change_impact as _analyze_change_impact
from .get_method_logic import get_method_logic as _get_method_logic
from .get_state_machine import get_state_machine as _get_state_machine
from .get_constraints import get_constraints as _get_constraints
from .get_access_control import get_access_control as _get_access_control
from .trace_compute_chain import trace_compute_chain as _trace_compute_chain
from .get_model_actions import get_model_actions as _get_model_actions
from .get_field_visibility import get_field_visibility as _get_field_visibility
from .trace_button_to_method import trace_button_to_method as _trace_button_to_method
from .get_http_routes import get_http_routes as _get_http_routes
from .build_index import build_index as _build_index
from .get_index_status import get_index_status as _get_index_status
from .get_model_graph import get_model_graph as _get_model_graph
from .get_project_context import get_project_context as _get_project_context
from .search_entities import search_odoo_entities as _search_odoo_entities
from .trace_path import trace_odoo_path as _trace_odoo_path
from .trace_business_flow import trace_business_flow as _trace_business_flow


def register_tools(
    mcp: FastMCP,
    get_db: Callable[[], Path],
    get_config: Callable,
) -> None:
    """Register all 19 tools onto the FastMCP instance.

    Args:
        mcp:        The FastMCP server instance.
        get_db:     Callable that returns the active project's SQLite db Path.
        get_config: Callable that returns the active ProjectConfig.
    """

    @mcp.tool(
        description=(
            "Get complete schema for an Odoo model: fields, types, compute methods, "
            "inheritance chain, and related models. "
            "Use compact=True for a one-line-per-field summary (~10× fewer tokens) "
            "when doing a quick scan. Use fields_limit to cap very large models."
        )
    )
    async def get_model_schema(
        model_name: str,
        compact: bool = False,
        fields_limit: int = 200,
    ) -> dict:
        """Get complete schema for an Odoo model."""
        return await _get_model_schema(model_name, get_db, compact=compact, fields_limit=fields_limit)

    @mcp.tool(
        description=(
            "Get the merged/resolved XML view for a model, showing all fields, "
            "buttons, and inherited view customizations."
        )
    )
    async def resolve_xml_view(
        model_name: str,
        view_type: str = "form",
    ) -> dict:
        """Resolve merged XML view for a model and view type."""
        return await _resolve_xml_view(model_name, view_type, get_db)

    @mcp.tool(
        description=(
            "Analyze the blast radius of changing a field or method: what views, "
            "methods, and other models depend on it."
        )
    )
    async def analyze_change_impact(
        model_name: str,
        field_name: Optional[str] = None,
        method_name: Optional[str] = None,
    ) -> dict:
        """Analyze change impact for a field or method on an Odoo model."""
        return await _analyze_change_impact(model_name, get_db, field_name, method_name)

    @mcp.tool(
        description=(
            "Get what a Python method does: decorators, state transitions it causes, "
            "ORM calls it makes, and constraints it enforces. "
            "include_source=True (default) adds file path and line number for direct source reading."
        )
    )
    async def get_method_logic(
        model_name: str,
        method_name: str,
        include_source: bool = True,
    ) -> dict:
        """Get logic and metadata for a Python method on an Odoo model."""
        return await _get_method_logic(model_name, method_name, get_db, include_source=include_source)

    @mcp.tool(
        description=(
            "Get the complete state machine for a model: all states, transitions, "
            "and the buttons/methods that trigger them."
        )
    )
    async def get_state_machine(model_name: str) -> dict:
        """Get the full state machine for an Odoo model."""
        return await _get_state_machine(model_name, get_db)

    @mcp.tool(
        description=(
            "Get all validation constraints for a model: Python @api.constrains, "
            "SQL constraints, and onchange validations."
        )
    )
    async def get_constraints(model_name: str) -> dict:
        """Get all constraints and onchange validators for an Odoo model."""
        return await _get_constraints(model_name, get_db)

    @mcp.tool(
        description=(
            "Get complete access control for a model: model-level ACLs, record rules, "
            "and field-level group restrictions."
        )
    )
    async def get_access_control(model_name: str) -> dict:
        """Get ACLs, record rules, and field group restrictions for an Odoo model."""
        return await _get_access_control(model_name, get_db)

    @mcp.tool(
        description=(
            "Trace how a computed field is calculated: its compute method, "
            "@api.depends fields, and @api.depends_context keys."
        )
    )
    async def trace_compute_chain(model_name: str, field_name: str) -> dict:
        """Trace the compute chain for a field on an Odoo model."""
        return await _trace_compute_chain(model_name, field_name, get_db)

    @mcp.tool(
        description=(
            "Get all actions, menus, cron jobs, and reports associated with an Odoo model."
        )
    )
    async def get_model_actions(model_name: str) -> dict:
        """Get actions, menus, cron jobs, and reports for an Odoo model."""
        return await _get_model_actions(model_name, get_db)

    @mcp.tool(
        description=(
            "Get when a field is visible, required, or readonly across all views "
            "— including group restrictions and state-based attrs."
        )
    )
    async def get_field_visibility(model_name: str, field_name: str) -> dict:
        """Get field visibility rules across all views for a model field."""
        return await _get_field_visibility(model_name, field_name, get_db)

    @mcp.tool(
        description=(
            "Trace a button in a view to the Python method it calls "
            "and what that method does."
        )
    )
    async def trace_button_to_method(view_xml_id: str, button_name: str) -> dict:
        """Trace a view button to its Python method or action."""
        return await _trace_button_to_method(view_xml_id, button_name, get_db)

    @mcp.tool(
        description=(
            "List HTTP/JSON-RPC routes exposed by Odoo modules, "
            "with auth requirements and path parameters."
        )
    )
    async def get_http_routes(
        module_name: Optional[str] = None,
        path_prefix: Optional[str] = None,
        auth_filter: Optional[str] = None,
    ) -> dict:
        """List HTTP routes, optionally filtered by module, path prefix, or auth type."""
        return await _get_http_routes(get_db, module_name, path_prefix, auth_filter)

    @mcp.tool(
        description=(
            "AI ENTRY POINT — always call this first at the start of every session. "
            "Returns a compact (≤400-token) overview of the Odoo knowledge graph: "
            "index health, entity counts, top models/modules, and a tool cheat-sheet. "
            "Pass focus_model to get quick facts + a suggested tool-call chain for that model."
        )
    )
    async def get_project_context(
        focus_model: Optional[str] = None,
    ) -> dict:
        """Compact AI entry point — index health, top models/modules, navigation guide."""
        return await _get_project_context(
            focus_model=focus_model,
            get_db=get_db,
            get_config=get_config,
        )

    @mcp.tool(
        description=(
            "Search the Odoo knowledge graph by name or keyword using FTS5. "
            "Finds models, fields, methods, views, and HTTP routes. "
            "Supports FTS5 operators: AND, OR, NOT, prefix* and \"phrase\". "
            "Filter by entity type(s) and/or module name. Returns up to 20 hits by default."
        )
    )
    async def search_odoo_entities(
        query: str,
        types: Optional[list[str]] = None,
        module: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search entities by name/keyword — FTS5 full-text search."""
        return await _search_odoo_entities(query, get_db, types=types, module=module, limit=limit)

    @mcp.tool(
        description=(
            "Walk the Odoo knowledge graph from a model, following ALL relationship edges "
            "up to `depth` hops (BFS). Collects connected models, fields, methods, states, "
            "and actions. edge_types filter: 'field_rel', 'inherit', 'compute', 'state', 'action'. "
            "Stops when token_budget is consumed so responses stay bounded. "
            "Returns nodes + edges suitable for visualization or further drilling."
        )
    )
    async def trace_odoo_path(
        start_model: str,
        depth: int = 2,
        edge_types: Optional[list[str]] = None,
        token_budget: int = 3000,
    ) -> dict:
        """Multi-hop BFS walk through the knowledge graph from a starting model."""
        return await _trace_odoo_path(
            start_model, get_db,
            depth=depth, edge_types=edge_types, token_budget=token_budget,
        )

    @mcp.tool(
        description=(
            "Get the current status of the Odoo knowledge graph index: "
            "whether it exists, when it was last built, entity counts, "
            "and how many modules have changed on disk (stale). "
            "Call this at the start of a session before using other tools."
        )
    )
    async def get_index_status() -> dict:
        """Check index health, freshness, and stale module count."""
        return await _get_index_status(get_db, get_config)

    @mcp.tool(
        description=(
            "Build or incrementally update the Odoo knowledge graph index. "
            "Call this when the user asks to index/reindex, when get_index_status "
            "reports stale modules, or when the user provides addons paths for the first time. "
            "Use force_rebuild=True only when the user explicitly asks for a full rebuild."
        )
    )
    async def build_index(
        addons_paths: Optional[list[str]] = None,
        force_rebuild: bool = False,
    ) -> dict:
        """Build or incrementally update the knowledge graph.

        Args:
            addons_paths:  Absolute paths to Odoo addons directories.
                           Omit to use the currently configured project paths.
            force_rebuild: Drop and rebuild from scratch (slow). Default: False.
        """
        return await _build_index(get_config, addons_paths, force_rebuild)

    @mcp.tool(
        description=(
            "Generate a visual graph of an Odoo model or module as a Mermaid diagram. "
            "graph_type options: 'relations' (Many2one/One2many/Many2many links), "
            "'state_machine' (state transitions), "
            "'inheritance' (_inherit/_inherits chain), "
            "'module_deps' (module dependency tree, pass module name as model_name). "
            "output_format: 'mermaid' (default, renderable diagram) or 'json' (raw data)."
        )
    )
    async def get_model_graph(
        model_name: str,
        graph_type: str = "relations",
        depth: int = 1,
        output_format: str = "mermaid",
    ) -> dict:
        """Generate Mermaid diagram or JSON graph for a model or module."""
        return await _get_model_graph(model_name, graph_type, depth, output_format, get_db)

    @mcp.tool(
        description=(
            "Trace the full business flow from a model: shows the root model's "
            "extension modules PLUS every One2many/Many2many-connected downstream model "
            "and what custom modules extend each one. "
            "Use this when get_project_context shows a model is extended but you need to "
            "understand the full pipeline (e.g. sale.order → stock.picking → account.move "
            "and which project-specific modules customise each step). "
            "Returns extending_modules with method names and custom_fields per module, "
            "plus a state_machine summary for each model that has one. "
            "Much faster than calling get_model_schema on every related model manually."
        )
    )
    async def trace_business_flow(start_model: str) -> dict:
        """Surface extending-module customisations across a model's downstream relational chain."""
        return await _trace_business_flow(start_model, get_db)
