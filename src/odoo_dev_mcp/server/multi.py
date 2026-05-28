"""Multi-project MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from ..config import ProjectConfig
from ..tools import register_tools
from ..prompts import register_prompts


class MultiProjectServer:
    """Manages multiple Odoo project configurations and serves them via a single FastMCP instance."""

    def __init__(self, projects: dict[str, ProjectConfig]) -> None:
        """
        Args:
            projects: Mapping of project name -> ProjectConfig.
        """
        self.projects: dict[str, ProjectConfig] = projects

    def resolve_project(
        self,
        explicit_name: Optional[str] = None,
        file_hint: Optional[str] = None,
    ) -> ProjectConfig:
        """Resolve which project to use.

        Priority:
        1. Explicit name — return projects[name] directly.
        2. File hint — find a project whose addons_paths contain the hint as prefix.
        3. Only 1 project — return it automatically.
        4. Error with list of available project names.

        Args:
            explicit_name: Caller-supplied project name (e.g. via a ``project`` tool parameter).
            file_hint: A file path string; the project whose addons dir is a prefix wins.

        Returns:
            The resolved :class:`ProjectConfig`.

        Raises:
            ValueError: When the project cannot be unambiguously determined.
        """
        # 1. Explicit name
        if explicit_name:
            if explicit_name not in self.projects:
                available = ", ".join(sorted(self.projects.keys()))
                raise ValueError(
                    f"Project '{explicit_name}' not found. "
                    f"Available projects: {available}"
                )
            return self.projects[explicit_name]

        # 2. File hint — check if it is prefixed by any project's addons path
        if file_hint:
            needle = str(Path(file_hint).resolve())
            best: Optional[ProjectConfig] = None
            best_len = 0
            for cfg in self.projects.values():
                for addons_path in cfg.all_paths:
                    ap_str = str(addons_path)
                    if needle.startswith(ap_str) and len(ap_str) > best_len:
                        best = cfg
                        best_len = len(ap_str)
            if best is not None:
                return best

        # 3. Only 1 project — return it
        if len(self.projects) == 1:
            return next(iter(self.projects.values()))

        # 4. Ambiguous
        available = ", ".join(sorted(self.projects.keys()))
        raise ValueError(
            f"Multiple projects available and none could be auto-selected. "
            f"Specify a project name. Available: {available}"
        )

    def create_server(self) -> FastMCP:
        """Create a FastMCP instance with all 12 tools registered plus a ``list_projects`` tool.

        Each of the 12 tools gets an extra ``project: Optional[str]`` parameter
        that is forwarded to :meth:`resolve_project` so callers can target a
        specific project.

        Returns:
            Configured :class:`FastMCP` instance.
        """
        project_names = ", ".join(sorted(self.projects.keys()))
        mcp = FastMCP(
            name="OdooDevMCP — Multi-Project",
            instructions=(
                "You are an expert Odoo development assistant with access to a complete "
                "knowledge graph of multiple Odoo codebases.\n\n"
                "ALWAYS start a session by calling get_project_context() first — it gives you "
                "index health, the top models, and a tool cheat-sheet so you know what to call next.\n\n"
                "Use the workflow prompts (analyze_odoo_model, debug_field_issue, plan_model_change, "
                "understand_business_flow, security_review) for guided multi-step analysis.\n\n"
                "Available projects: " + project_names + "\n\n"
                "Pass the ``project`` parameter to any tool to target a specific project. "
                "If omitted and only one project is configured the server will use it "
                "automatically; otherwise you must specify the project name."
            ),
        )

        server = self  # capture for closures

        # ── Helper that builds a get_db callable bound to a chosen project ────

        def make_get_db(explicit_project: Optional[str]):
            """Return a callable that resolves the db_path for the given project name."""
            config = server.resolve_project(explicit_project, None)
            return config.db_path

        # ── Import the underlying tool implementations directly ───────────────

        from ..tools.get_model_schema import get_model_schema as _get_model_schema
        from ..tools.resolve_xml_view import resolve_xml_view as _resolve_xml_view
        from ..tools.analyze_change_impact import analyze_change_impact as _analyze_change_impact
        from ..tools.get_method_logic import get_method_logic as _get_method_logic
        from ..tools.get_state_machine import get_state_machine as _get_state_machine
        from ..tools.get_constraints import get_constraints as _get_constraints
        from ..tools.get_access_control import get_access_control as _get_access_control
        from ..tools.trace_compute_chain import trace_compute_chain as _trace_compute_chain
        from ..tools.get_model_actions import get_model_actions as _get_model_actions
        from ..tools.get_field_visibility import get_field_visibility as _get_field_visibility
        from ..tools.trace_button_to_method import trace_button_to_method as _trace_button_to_method
        from ..tools.get_http_routes import get_http_routes as _get_http_routes
        from ..tools.build_index import build_index as _build_index
        from ..tools.get_index_status import get_index_status as _get_index_status
        from ..tools.get_model_graph import get_model_graph as _get_model_graph
        from ..tools.get_project_context import get_project_context as _get_project_context
        from ..tools.search_entities import search_odoo_entities as _search_odoo_entities
        from ..tools.trace_path import trace_odoo_path as _trace_odoo_path

        def make_get_config(explicit_project: Optional[str]):
            cfg = server.resolve_project(explicit_project, None)
            return lambda: cfg

        # ── Register multi-project-aware wrappers ─────────────────────────────

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
            project: Optional[str] = None,
        ) -> dict:
            """Get complete schema for an Odoo model."""
            db_path = make_get_db(project)
            return await _get_model_schema(model_name, lambda: db_path, compact=compact, fields_limit=fields_limit)

        @mcp.tool(
            description=(
                "Get the merged/resolved XML view for a model, showing all fields, "
                "buttons, and inherited view customizations."
            )
        )
        async def resolve_xml_view(
            model_name: str,
            view_type: str = "form",
            project: Optional[str] = None,
        ) -> dict:
            """Resolve merged XML view for a model and view type."""
            db_path = make_get_db(project)
            return await _resolve_xml_view(model_name, view_type, lambda: db_path)

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
            project: Optional[str] = None,
        ) -> dict:
            """Analyze change impact for a field or method on an Odoo model."""
            db_path = make_get_db(project)
            return await _analyze_change_impact(model_name, lambda: db_path, field_name, method_name)

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
            project: Optional[str] = None,
        ) -> dict:
            """Get logic and metadata for a Python method on an Odoo model."""
            db_path = make_get_db(project)
            return await _get_method_logic(model_name, method_name, lambda: db_path, include_source=include_source)

        @mcp.tool(
            description=(
                "Get the complete state machine for a model: all states, transitions, "
                "and the buttons/methods that trigger them."
            )
        )
        async def get_state_machine(
            model_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Get the full state machine for an Odoo model."""
            db_path = make_get_db(project)
            return await _get_state_machine(model_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Get all validation constraints for a model: Python @api.constrains, "
                "SQL constraints, and onchange validations."
            )
        )
        async def get_constraints(
            model_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Get all constraints and onchange validators for an Odoo model."""
            db_path = make_get_db(project)
            return await _get_constraints(model_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Get complete access control for a model: model-level ACLs, record rules, "
                "and field-level group restrictions."
            )
        )
        async def get_access_control(
            model_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Get ACLs, record rules, and field group restrictions for an Odoo model."""
            db_path = make_get_db(project)
            return await _get_access_control(model_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Trace how a computed field is calculated: its compute method, "
                "@api.depends fields, and @api.depends_context keys."
            )
        )
        async def trace_compute_chain(
            model_name: str,
            field_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Trace the compute chain for a field on an Odoo model."""
            db_path = make_get_db(project)
            return await _trace_compute_chain(model_name, field_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Get all actions, menus, cron jobs, and reports associated with an Odoo model."
            )
        )
        async def get_model_actions(
            model_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Get actions, menus, cron jobs, and reports for an Odoo model."""
            db_path = make_get_db(project)
            return await _get_model_actions(model_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Get when a field is visible, required, or readonly across all views "
                "— including group restrictions and state-based attrs."
            )
        )
        async def get_field_visibility(
            model_name: str,
            field_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Get field visibility rules across all views for a model field."""
            db_path = make_get_db(project)
            return await _get_field_visibility(model_name, field_name, lambda: db_path)

        @mcp.tool(
            description=(
                "Trace a button in a view to the Python method it calls "
                "and what that method does."
            )
        )
        async def trace_button_to_method(
            view_xml_id: str,
            button_name: str,
            project: Optional[str] = None,
        ) -> dict:
            """Trace a view button to its Python method or action."""
            db_path = make_get_db(project)
            return await _trace_button_to_method(view_xml_id, button_name, lambda: db_path)

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
            project: Optional[str] = None,
        ) -> dict:
            """List HTTP routes, optionally filtered by module, path prefix, or auth type."""
            db_path = make_get_db(project)
            return await _get_http_routes(lambda: db_path, module_name, path_prefix, auth_filter)

        # ── Tools: search + trace ─────────────────────────────────────────────

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
            project: Optional[str] = None,
        ) -> dict:
            """Search entities by name/keyword — FTS5 full-text search."""
            db_path = make_get_db(project)
            return await _search_odoo_entities(query, lambda: db_path, types=types, module=module, limit=limit)

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
            project: Optional[str] = None,
        ) -> dict:
            """Multi-hop BFS walk through the knowledge graph from a starting model."""
            db_path = make_get_db(project)
            return await _trace_odoo_path(
                start_model, lambda: db_path,
                depth=depth, edge_types=edge_types, token_budget=token_budget,
            )

        # ── Tool: get_project_context ─────────────────────────────────────────

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
            project: Optional[str] = None,
        ) -> dict:
            """Compact AI entry point — index health, top models/modules, navigation guide."""
            db_path = make_get_db(project)
            get_config = make_get_config(project)
            return await _get_project_context(
                focus_model=focus_model,
                get_db=lambda: db_path,
                get_config=get_config,
            )

        # ── Tool: get_index_status ────────────────────────────────────────────

        @mcp.tool(
            description=(
                "Get the current status of the Odoo knowledge graph index: "
                "whether it exists, when it was last built, entity counts, "
                "and how many modules have changed on disk (stale)."
            )
        )
        async def get_index_status(project: Optional[str] = None) -> dict:
            """Check index health, freshness, and stale module count."""
            db_path = make_get_db(project)
            get_config = make_get_config(project)
            return await _get_index_status(lambda: db_path, get_config)

        # ── Tool: build_index ─────────────────────────────────────────────────

        @mcp.tool(
            description=(
                "Build or incrementally update the Odoo knowledge graph index. "
                "Call this when the user asks to index/reindex, or when get_index_status "
                "reports stale modules."
            )
        )
        async def build_index(
            addons_paths: Optional[list[str]] = None,
            force_rebuild: bool = False,
            project: Optional[str] = None,
        ) -> dict:
            """Build or incrementally update the knowledge graph."""
            get_config = make_get_config(project)
            return await _build_index(get_config, addons_paths, force_rebuild)

        # ── Tool: get_model_graph ─────────────────────────────────────────────

        @mcp.tool(
            description=(
                "Generate a visual graph of an Odoo model or module as a Mermaid diagram. "
                "graph_type: 'relations' (field links), 'state_machine' (state transitions), "
                "'inheritance' (_inherit chain), 'module_deps' (dependency tree). "
                "output_format: 'mermaid' (default) or 'json'."
            )
        )
        async def get_model_graph(
            model_name: str,
            graph_type: str = "relations",
            depth: int = 1,
            output_format: str = "mermaid",
            project: Optional[str] = None,
        ) -> dict:
            """Generate Mermaid diagram or JSON graph for a model or module."""
            db_path = make_get_db(project)
            return await _get_model_graph(model_name, graph_type, depth, output_format, lambda: db_path)

        # ── Tool: list_projects ───────────────────────────────────────────────

        @mcp.tool(
            description=(
                "List all available Odoo projects registered in this multi-project server, "
                "including their index status and addons paths."
            )
        )
        async def list_projects() -> dict:
            """List all available projects and their status."""
            result = {}
            for name, cfg in sorted(server.projects.items()):
                db_path = cfg.db_path
                index_exists = db_path.exists()
                index_size_mb: Optional[float] = None
                if index_exists:
                    try:
                        index_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)
                    except OSError:
                        pass
                result[name] = {
                    "name": name,
                    "root_path": str(cfg.root_path),
                    "addons_paths": [str(p) for p in cfg.all_paths],
                    "db_path": str(db_path),
                    "index_exists": index_exists,
                    "index_size_mb": index_size_mb,
                    "js_parsing": cfg.js_parsing,
                    "watch_enabled": cfg.watch_enabled,
                }
            return {"projects": result, "count": len(result)}

        # ── Workflow prompts ──────────────────────────────────────────────────
        register_prompts(mcp, project_name=project_names)

        return mcp
