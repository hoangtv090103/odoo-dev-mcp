"""Single-project MCP server."""

from pathlib import Path
from fastmcp import FastMCP
from ..tools import register_tools
from ..prompts import register_prompts
from ..config import ProjectConfig


def create_single_server(config: ProjectConfig) -> FastMCP:
    """
    Create a FastMCP server for a single project.
    Returns a configured FastMCP instance with all 18 tools and 5 prompts registered.
    """
    mcp = FastMCP(
        name=f"OdooDevMCP — {config.name}",
        instructions=(
            "You are an expert Odoo development assistant with access to a complete "
            "knowledge graph of the Odoo codebase. You can analyze models, fields, methods, views, "
            "HTTP routes, security rules, state machines, and cross-layer dependencies.\n\n"
            "ALWAYS start a session by calling get_project_context() first — it gives you "
            "index health, the top models, and a tool cheat-sheet so you know what to call next.\n\n"
            "Use the workflow prompts (analyze_odoo_model, debug_field_issue, plan_model_change, "
            "understand_business_flow, security_review) for guided multi-step analysis.\n\n"
            "Current project: " + config.name
        ),
    )

    db_path = config.db_path
    register_tools(mcp, lambda: db_path, lambda: config)
    register_prompts(mcp, project_name=config.name)
    return mcp
