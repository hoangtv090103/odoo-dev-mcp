"""
CLI entry point for odoo-dev-mcp.

Commands:
  init        Interactive project setup
  index       Build / rebuild the index
  serve       Start the MCP server (stdio)
  install     Write IDE config files
  uninstall   Remove IDE config files
  status      Show project status
  stats       Show index statistics
  query       Call a single tool from the CLI
  project     Sub-group: add / list / info / remove / use
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ago(ts: Optional[float]) -> str:
    """Return a human-friendly 'X ago' string from a Unix timestamp, or 'never'."""
    if ts is None:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _fmt_size(path: Path) -> str:
    """Return human-friendly file size for *path*, or '—' if unavailable."""
    try:
        size = path.stat().st_size
        if size < 1024:
            return f"{size}B"
        if size < 1024 ** 2:
            return f"{size // 1024}KB"
        return f"{size // (1024 ** 2)}MB"
    except OSError:
        return "—"


def _split_paths(raw: str) -> list[Path]:
    """Split a path string on ':' or ',' and resolve each entry."""
    sep = "," if "," in raw and ":" not in raw else ":"
    return [Path(p.strip()).expanduser().resolve() for p in raw.split(sep) if p.strip()]


def _ensure_gitignore_entry(directory: Path, entry: str) -> None:
    """Add *entry* to ``<directory>/.gitignore`` if it is not already present.

    Creates the file when it doesn't exist.  Prints a status line to the console.
    """
    gitignore = directory / ".gitignore"
    # Normalise to bare name for comparison
    bare = entry.strip().rstrip("/")

    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        # Already present?  Match with or without trailing slash.
        if any(ln.strip().rstrip("/") == bare for ln in lines):
            return
        # Append
        separator = "\n" if lines and lines[-1].strip() else ""
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(f"{separator}{entry}\n")
        console.print(f"[green]✓[/green] Added [bold]{entry}[/bold] to .gitignore")
    else:
        gitignore.write_text(f"{entry}\n", encoding="utf-8")
        console.print(f"[green]✓[/green] Created .gitignore with [bold]{entry}[/bold]")


def _is_index_complete(db_path: Path) -> bool:
    """Return True if the DB exists AND has a completed indexed_at timestamp.

    A DB file can exist but be incomplete if a previous indexing run was
    interrupted (crash, process kill) before _write_metadata() was called.
    With the new atomic-write strategy, this should only happen for indexes
    built with older versions of the code.  Going forward, ``index.db`` is
    only written once all phases are done (via atomic rename from index.db.new).

    Also returns False when only the ``.new`` temp file exists (i.e. a full
    rebuild was interrupted before the rename could happen).
    """
    if not db_path.exists():
        return False
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'indexed_at'"
        ).fetchone()
        conn.close()
        return row is not None and bool(row[0])
    except Exception:
        return False


def _auto_index(config, reset: bool = False) -> None:
    """Run indexing silently to stderr — called when serve starts with no complete index."""
    from .indexer.pipeline import run_full_index, cleanup_stale_tmp

    # Clean up any leftover .new file from a previous interrupted full rebuild
    # before starting — avoids confusion and frees disk space.
    cleanup_stale_tmp(config.db_path)

    if reset:
        print("OdooDevMCP: incomplete index detected — rebuilding from scratch...", file=sys.stderr, flush=True)
    else:
        print("OdooDevMCP: index not found — building now...", file=sys.stderr, flush=True)

    def _progress(step: int, total: int, message: str) -> None:
        print(f"OdooDevMCP: [{step}/{total}] {message}", file=sys.stderr, flush=True)

    try:
        # If resetting (incomplete index): full rebuild.
        # If fresh DB: full index (incremental=False).
        # If DB exists and is complete: incremental (should not be called, but handle gracefully).
        incremental = (not reset) and config.db_path.exists()
        result = run_full_index(config, reset=reset, incremental=incremental, progress_cb=_progress)
        if result.errors:
            for err in result.errors:
                print(f"OdooDevMCP: warning: {err}", file=sys.stderr, flush=True)
        print(
            f"OdooDevMCP: index ready — "
            f"{result.modules_count} modules, {result.models_count} models, "
            f"{result.fields_count} fields, {result.routes_count} routes, "
            f"{result.views_count} views ({result.duration_seconds:.1f}s)",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:
        print(f"OdooDevMCP: auto-index failed: {exc}", file=sys.stderr, flush=True)


def _resolve_config(addons_path: Optional[str], start_dir: Optional[Path] = None):
    """Resolve project config with consistent error handling."""
    from .config import resolve_project_config
    try:
        return resolve_project_config(addons_path, start_dir or Path.cwd())
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """OdooDevMCP — Odoo codebase knowledge graph for AI assistants."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@main.command("init")
@click.option("--name", default=None, help="Project name (default: current directory name).")
@click.option("--yes", "-y", is_flag=True, default=False, help="Non-interactive: accept all defaults.")
def cmd_init(name: Optional[str], yes: bool) -> None:
    """Interactive project setup — creates .odoo-dev-mcp.toml in the current directory."""
    from .config import create_default_toml

    cwd = Path.cwd()

    console.print(Panel("[bold cyan]OdooDevMCP — Project Setup[/bold cyan]", expand=False))

    # ── Project name ──────────────────────────────────────────────────────────
    default_name = name or cwd.name
    if yes or name:
        project_name = default_name
    else:
        project_name = click.prompt("Project name", default=default_name)

    # ── Auto-scan for addons directories ─────────────────────────────────────
    console.print("\n[dim]Scanning for addons directories (up to 3 levels deep)...[/dim]")
    found_paths: list[Path] = _scan_addons_paths(cwd)

    if not found_paths:
        console.print("[yellow]No addons directories found automatically.[/yellow]")
        if not yes:
            manual = click.prompt(
                "Enter addons paths (colon-separated)",
                default="",
            )
            if manual.strip():
                found_paths = _split_paths(manual)
    else:
        console.print(f"\nFound [green]{len(found_paths)}[/green] potential addons director{'y' if len(found_paths) == 1 else 'ies'}:")
        for p in found_paths:
            try:
                display = "./" + str(p.relative_to(cwd))
            except ValueError:
                display = str(p)
            console.print(f"  [green]✓[/green] {display}")

        if not yes:
            confirmed = click.confirm("\nUse these paths?", default=True)
            if not confirmed:
                manual = click.prompt(
                    "Enter addons paths (colon or comma separated, relative or absolute)",
                    default="",
                )
                if manual.strip():
                    found_paths = _split_paths(manual)
                else:
                    found_paths = []

    if not found_paths:
        console.print("[red]No addons paths configured. Aborting.[/red]")
        raise SystemExit(1)

    # ── Options ───────────────────────────────────────────────────────────────
    if yes:
        watch_enabled = True
        js_parsing = False
    else:
        watch_enabled = click.confirm("\nEnable file watching (auto-reindex on save)?", default=True)
        js_parsing = click.confirm("Enable JavaScript analysis (slower, requires tree-sitter)?", default=False)

    # ── Write config ──────────────────────────────────────────────────────────
    config_path = cwd / ".odoo-dev-mcp.toml"
    if config_path.exists() and not yes:
        overwrite = click.confirm(f"\n[yellow].odoo-dev-mcp.toml already exists.[/yellow] Overwrite?", default=False)
        if not overwrite:
            console.print("[dim]Aborted.[/dim]")
            raise SystemExit(0)

    toml_content = create_default_toml(cwd, project_name, found_paths)

    # Inject watch/js options into the generated content
    toml_content = toml_content.replace(
        "enabled = true",
        f"enabled = {'true' if watch_enabled else 'false'}",
    )
    toml_content = toml_content.replace(
        "js_parsing = false",
        f"js_parsing = {'true' if js_parsing else 'false'}",
    )

    config_path.write_text(toml_content, encoding="utf-8")

    console.print(f"\n[green]✓[/green] Created [bold]{config_path}[/bold]")

    # ── .gitignore — add .odoo-dev-mcp/ entry ───────────────────────────────────
    _ensure_gitignore_entry(cwd, ".odoo-dev-mcp/")

    # ── Next steps ────────────────────────────────────────────────────────────
    console.print(
        Panel(
            "[bold]Next steps:[/bold]\n\n"
            "  1. [cyan]odoo-dev-mcp index[/cyan]          — Build the index\n"
            "  2. [cyan]odoo-dev-mcp serve[/cyan]           — Start the MCP server\n"
            "  3. [cyan]odoo-dev-mcp install --platform claude-code[/cyan]  — Configure Claude Code\n"
            "  4. [cyan]odoo-dev-mcp status[/cyan]          — Check project status",
            title="Setup complete",
            expand=False,
        )
    )


def _scan_addons_paths(root: Path, max_depth: int = 3) -> list[Path]:
    """Walk *root* up to *max_depth* levels, find directories with 10+ addon subdirs."""
    candidate_parents: dict[Path, int] = {}

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = list(directory.iterdir())
        except PermissionError:
            return
        manifest_count = sum(
            1
            for c in children
            if c.is_dir() and (c / "__manifest__.py").is_file()
        )
        if manifest_count >= 10:
            candidate_parents[directory] = manifest_count
        for child in children:
            if child.is_dir() and not child.name.startswith(".") and child.name != "__pycache__":
                _walk(child, depth + 1)

    _walk(root, 0)
    # Sort by manifest count descending, then path
    return sorted(candidate_parents.keys(), key=lambda p: (-candidate_parents[p], str(p)))


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

@main.command("index")
@click.option("--force", "-f", is_flag=True, default=False, help="Drop and rebuild the index from scratch.")
@click.option("--incremental", "-i", is_flag=True, default=False, help="Only re-index modules whose files changed (default when index already exists).")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths (overrides .toml).")
@click.option("--project-name", "project_name", default=None, help="Override project name.")
def cmd_index(force: bool, incremental: bool, addons_path: Optional[str], project_name: Optional[str]) -> None:
    """Build or rebuild the search index for the current project."""
    from .config import resolve_project_config
    from .indexer.pipeline import run_full_index
    from .registry import ProjectRegistry

    config = _resolve_config(addons_path)

    if project_name:
        config.name = project_name

    console.print(
        Panel(
            f"[bold]Indexing project:[/bold] {config.name}\n"
            f"[bold]Database:[/bold] {config.db_path}\n"
            f"[bold]Addons paths:[/bold]\n"
            + "\n".join(f"  • {p}" for p in config.all_paths),
            title="OdooDevMCP — Index",
            expand=False,
        )
    )

    # Auto-enable incremental if index exists and --force not set
    use_incremental = incremental or (not force and config.db_path.exists())

    if force:
        console.print("[yellow]--force: dropping and rebuilding existing index.[/yellow]")
    elif use_incremental:
        console.print("[dim]Incremental mode: only re-indexing changed modules.[/dim]")

    result_holder: dict = {}
    error_holder: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting...", total=8)

        def progress_cb(step: int, total: int, message: str) -> None:
            progress.update(task, completed=step, total=total, description=message)

        try:
            result = run_full_index(config, reset=force, incremental=use_incremental, progress_cb=progress_cb)
            result_holder["result"] = result
            if result.errors:
                error_holder.extend(result.errors)
        except Exception as exc:
            console.print(f"[red]Indexing failed:[/red] {exc}")
            raise SystemExit(1)

    result = result_holder.get("result")
    if result is None:
        console.print("[red]Indexing failed — no result returned.[/red]")
        raise SystemExit(1)

    # ── Summary table ─────────────────────────────────────────────────────────
    table = Table(title="Index Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")

    table.add_row("Modules", str(result.modules_count))
    table.add_row("Models", str(result.models_count))
    table.add_row("Fields", str(result.fields_count))
    table.add_row("Methods", str(result.methods_count))
    table.add_row("Views", str(result.views_count))
    table.add_row("HTTP Routes", str(result.routes_count))
    table.add_row("Duration", f"{result.duration_seconds:.1f}s")

    console.print(table)

    if error_holder:
        console.print(f"\n[yellow]Warnings/Errors ({len(error_holder)}):[/yellow]")
        for err in error_holder[:10]:
            console.print(f"  [yellow]•[/yellow] {err}")
        if len(error_holder) > 10:
            console.print(f"  [dim]... and {len(error_holder) - 10} more[/dim]")

    # ── Update registry ───────────────────────────────────────────────────────
    try:
        registry = ProjectRegistry()
        registry.add(config)
        registry.update_last_indexed(config.name)
        console.print(f"\n[green]✓[/green] Registry updated.")
    except Exception as exc:
        console.print(f"[yellow]Warning: Could not update registry: {exc}[/yellow]")

    console.print(f"\n[green]✓[/green] Index complete — [bold]{config.db_path}[/bold]")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@main.command("serve")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths.")
@click.option("--all", "serve_all", is_flag=True, default=False, help="Serve all projects from registry in multi-project mode.")
@click.option("--project", "project_name", default=None, help="Specific project name (from registry) to serve.")
def cmd_serve(addons_path: Optional[str], serve_all: bool, project_name: Optional[str]) -> None:
    """Start the MCP server (stdio transport)."""
    from .config import resolve_project_config, ProjectConfig
    from .registry import ProjectRegistry

    if serve_all:
        # ── Multi-project mode ────────────────────────────────────────────────
        from .server.multi import MultiProjectServer

        registry = ProjectRegistry()
        entries = registry.list_all()
        if not entries:
            console.print(
                "[red]Error:[/red] No projects in registry. "
                "Run [bold]odoo-dev-mcp project add[/bold] or [bold]odoo-dev-mcp index[/bold] first.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        projects: dict[str, ProjectConfig] = {}
        for entry in entries:
            from .config import load_project_config, AddonsPathEntry
            if entry.config_path:
                try:
                    cfg = load_project_config(Path(entry.config_path))
                    projects[cfg.name] = cfg
                    continue
                except Exception:
                    pass
            # Reconstruct minimal config from registry entry
            ap_entries = [
                AddonsPathEntry(path=Path(ap["path"]), label=ap.get("label") or "")
                for ap in entry.addons_paths
            ]
            cfg = ProjectConfig(
                name=entry.name,
                addons_paths=ap_entries,
                root_path=Path(entry.root_path),
            )
            projects[cfg.name] = cfg

        if project_name and project_name not in projects:
            available = ", ".join(sorted(projects.keys()))
            console.print(
                f"[red]Error:[/red] Project '{project_name}' not found. Available: {available}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        if project_name:
            projects = {project_name: projects[project_name]}

        import threading
        ms = MultiProjectServer(projects)
        mcp = ms.create_server()

        # Auto-index any projects whose index is missing or incomplete.
        # "Incomplete" means the DB file exists but indexed_at was never written
        # (happens when a previous indexing run was killed before finishing).
        for name, cfg in projects.items():
            complete = _is_index_complete(cfg.db_path)
            if not complete:
                needs_reset = cfg.db_path.exists()  # exists but incomplete → reset
                msg = (
                    f"OdooDevMCP: project '{name}' has incomplete index — rebuilding..."
                    if needs_reset else
                    f"OdooDevMCP: project '{name}' has no index — building in background..."
                )
                print(msg, file=sys.stderr, flush=True)
                t = threading.Thread(
                    target=_auto_index, args=(cfg,), kwargs={"reset": needs_reset}, daemon=True
                )
                t.start()

        mcp.run(transport="stdio")

    else:
        # ── Single-project mode ───────────────────────────────────────────────
        import threading
        from .server.single import create_single_server

        config = _resolve_config(addons_path)

        if not _is_index_complete(config.db_path):
            # Index in background so MCP server can start immediately and
            # respond to the Claude Code initialize handshake without timing out.
            # Tools will return a "index not ready" message until indexing completes.
            # If the DB exists but is incomplete (prior interrupted run), reset it.
            needs_reset = config.db_path.exists()
            if needs_reset:
                print(
                    "OdooDevMCP: incomplete index detected (prior run interrupted?) — rebuilding...",
                    file=sys.stderr, flush=True,
                )
            t = threading.Thread(
                target=_auto_index, args=(config,), kwargs={"reset": needs_reset}, daemon=True
            )
            t.start()

        mcp = create_single_server(config)
        mcp.run(transport="stdio")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

_CURSOR_CONFIG = """\
{
  "mcpServers": {
    "odoo-dev-mcp": {
      "command": "odoo-dev-mcp",
      "args": ["serve"]
    }
  }
}
"""

_CLAUDE_CODE_CONFIG = """\
{
  "mcpServers": {
    "odoo-dev-mcp": {
      "command": "odoo-dev-mcp",
      "args": ["serve"],
      "type": "stdio"
    }
  }
}
"""

_VSCODE_SETTINGS_SNIPPET = """\
{
  "mcp.servers": {
    "odoo-dev-mcp": {
      "command": "odoo-dev-mcp",
      "args": ["serve"],
      "transport": "stdio"
    }
  }
}
"""


_SKILL_MD_CONTENT = """\
---
name: odoo-dev-mcp
description: >
  Use whenever the user asks about this Odoo codebase: model structure,
  fields, methods, business flows, state machines, security rules, HTTP
  controllers, XML views, compute chains, or change impact. This project
  has a pre-built knowledge graph index — always call get_project_context()
  first instead of reading source files or using generic code-search tools.
---

This project uses the **odoo-dev-mcp** MCP server with a pre-built Odoo
knowledge graph index. Do NOT grep source files or use generic code-search
tools for Odoo structure questions — use the tools below instead.

## Step 1 — Always call this first

```
get_project_context()
```

Returns index health, top models, top modules, and the recommended tool chain.
To zoom in on a specific model: `get_project_context(focus_model="sale.order")`
For current project stats (modules, models, fields): call `get_index_status()`.

## Tool Quick Reference

| Goal | Tool |
|---|---|
| Entry point | `get_project_context(focus_model=...)` |
| Fields & methods | `get_model_schema(model)` |
| Compact overview | `get_model_schema(model, compact=True)` |
| Search anything | `search_odoo_entities(query, types=[])` |
| State machine | `get_state_machine(model)` |
| Compute chain | `trace_compute_chain(model, field)` |
| Change blast radius | `analyze_change_impact(model)` |
| Security / ACL | `get_access_control(model)` |
| HTTP controllers | `get_http_routes(module=...)` |
| Relationship graph | `trace_odoo_path(model, depth=2)` |
| Merged XML view | `resolve_xml_view(model, "form")` |
| Index freshness | `get_index_status()` |
| Rebuild index | `build_index()` |

## Guided Workflow Prompts

| Prompt | When to use |
|---|---|
| `analyze_odoo_model(model)` | Full model deep-dive |
| `debug_field_issue(model, field)` | Wrong / missing field value |
| `plan_model_change(model, description)` | Pre-change impact assessment |
| `understand_business_flow(model)` | End-to-end lifecycle trace |
| `security_review(model)` | ACL, record rules, HTTP exposure |

## Notes

- Index not built yet? Run `odoo-dev-mcp index` in the terminal.
- Index stale? Run `odoo-dev-mcp index` or call `build_index()`.
- `get_project_context()` warns automatically when modules changed on disk.
"""


def _write_skill_files(project_root: Path, force: bool = False) -> list[str]:
    """Write Agent Skill files to both Claude Code and standard locations.

    Writes the same SKILL.md to:
      .claude/skills/odoo-dev-mcp/SKILL.md  — Claude Code
      .agents/skills/odoo-dev-mcp/SKILL.md  — VS Code Copilot, Cursor,
                                               OpenAI Codex, and any tool
                                               following agentskills.io

    Returns a list of paths written.
    """
    skill_dirs = [
        project_root / ".claude" / "skills" / "odoo-dev-mcp",
        project_root / ".agents" / "skills" / "odoo-dev-mcp",
    ]
    written: list[str] = []
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists() and not force:
            written.append(str(skill_file))  # already present, count as written
            continue
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(_SKILL_MD_CONTENT, encoding="utf-8")
            written.append(str(skill_file))
        except OSError as exc:
            console.print(f"[yellow]Warning: could not write {skill_file}: {exc}[/yellow]")
    return written


def _merge_json_mcp_servers(existing_path: Path, new_content: str) -> str:
    """Merge new MCP server config into existing JSON, preserving other entries."""
    new_data = json.loads(new_content)
    if not existing_path.exists():
        return json.dumps(new_data, indent=2)
    try:
        existing_data = json.loads(existing_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.dumps(new_data, indent=2)

    if "mcpServers" not in existing_data:
        existing_data["mcpServers"] = {}
    existing_data["mcpServers"].update(new_data.get("mcpServers", {}))

    if "mcp.servers" not in existing_data:
        existing_data["mcp.servers"] = {}
    if "mcp.servers" in new_data:
        existing_data["mcp.servers"].update(new_data.get("mcp.servers", {}))

    return json.dumps(existing_data, indent=2)


@main.command("install")
@click.option(
    "--platform",
    "platform",
    required=True,
    type=click.Choice(["cursor", "claude-code", "vscode"], case_sensitive=False),
    help="IDE / tool to configure.",
)
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths to embed in the config.")
@click.option("--force", "-f", is_flag=True, default=False, help="Overwrite existing config without prompting.")
def cmd_install(platform: str, addons_path: Optional[str], force: bool) -> None:
    """Write IDE/tool MCP config files for the current project."""
    from .config import find_project_config, load_project_config

    cwd = Path.cwd()
    platform = platform.lower()

    # ── Resolve addons paths ──────────────────────────────────────────────────
    # Priority: CLI flag → .odoo-dev-mcp.toml → interactive prompt
    resolved_paths_str: Optional[str] = addons_path

    if not resolved_paths_str:
        # Try to read from existing .odoo-dev-mcp.toml
        toml_path = find_project_config(cwd)
        if toml_path:
            try:
                cfg = load_project_config(toml_path)
                resolved_paths_str = ",".join(str(p) for p in cfg.all_paths)
                console.print(
                    f"[dim]Using addons paths from [bold]{toml_path.name}[/bold]:[/dim]"
                )
                for p in cfg.all_paths:
                    console.print(f"  [green]✓[/green] {p}")
            except Exception as exc:
                console.print(f"[yellow]Warning: could not read .odoo-dev-mcp.toml: {exc}[/yellow]")

    if not resolved_paths_str:
        # Interactive prompt — paths are required for the server to work
        console.print(
            "\n[bold yellow]Addons paths are required[/bold yellow] so the MCP server knows "
            "which Odoo code to index."
        )
        raw = click.prompt(
            "Enter addons paths (colon or comma separated, relative or absolute)",
            default="",
        ).strip()
        if raw:
            # Normalise to comma-separated absolute paths
            resolved = _split_paths(raw)
            resolved_paths_str = ",".join(str(p) for p in resolved)
        else:
            console.print(
                "[yellow]No addons paths provided. "
                "The server will try to find a .odoo-dev-mcp.toml at runtime.[/yellow]"
            )

    # Build serve args — always embed paths when resolved
    serve_args: list = ["serve"]
    if resolved_paths_str:
        serve_args += ["--addons-path", resolved_paths_str]

    def _make_config(template: dict) -> str:
        # Inject serve_args into the template
        for server in template.get("mcpServers", {}).values():
            server["args"] = serve_args
        for server in template.get("mcp.servers", {}).values():
            server["args"] = serve_args
        return json.dumps(template, indent=2)

    if platform == "cursor":
        target = cwd / ".cursor" / "mcp.json"
        content = _make_config(json.loads(_CURSOR_CONFIG))
    elif platform == "claude-code":
        target = cwd / ".mcp.json"
        content = _make_config(json.loads(_CLAUDE_CODE_CONFIG))
    elif platform == "vscode":
        target = cwd / ".vscode" / "settings.json"
        content = _make_config(json.loads(_VSCODE_SETTINGS_SNIPPET))
    else:
        console.print(f"[red]Unknown platform: {platform}[/red]")
        raise SystemExit(1)

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not force:
        console.print(f"[yellow]{target}[/yellow] already exists.")
        overwrite = click.confirm("Overwrite?", default=False)
        if not overwrite:
            console.print("[dim]Aborted.[/dim]")
            raise SystemExit(0)

    # Merge rather than blindly overwrite for JSON configs
    if target.exists() and target.suffix == ".json":
        merged = _merge_json_mcp_servers(target, content)
        target.write_text(merged + "\n", encoding="utf-8")
    else:
        target.write_text(content + "\n", encoding="utf-8")

    console.print(f"[green]✓[/green] Written: [bold]{target}[/bold]")

    # ── Write Agent Skill files ───────────────────────────────────────────
    # SKILL.md is an IDE integration artefact — belongs in install, not index.
    # Written to both .claude/skills/ (Claude Code) and .agents/skills/
    # (VS Code Copilot, Cursor, OpenAI Codex — agentskills.io standard).
    skill_paths = _write_skill_files(cwd, force=force)
    for sp in skill_paths:
        rel = Path(sp).relative_to(cwd) if Path(sp).is_relative_to(cwd) else sp
        console.print(f"[green]✓[/green] Skill:   [bold]{rel}[/bold]")

    console.print(
        f"\n[dim]Restart {platform} / reload the window to pick up the new MCP server.[/dim]"
    )


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------

@main.command("uninstall")
@click.option(
    "--platform",
    "platform",
    required=True,
    type=click.Choice(["cursor", "claude-code", "vscode"], case_sensitive=False),
    help="IDE / tool config to remove.",
)
def cmd_uninstall(platform: str) -> None:
    """Remove MCP config files for a platform."""
    cwd = Path.cwd()
    platform = platform.lower()

    if platform == "cursor":
        target = cwd / ".cursor" / "mcp.json"
    elif platform == "claude-code":
        target = cwd / ".mcp.json"
    elif platform == "vscode":
        target = cwd / ".vscode" / "settings.json"
    else:
        console.print(f"[red]Unknown platform: {platform}[/red]")
        raise SystemExit(1)

    if not target.exists():
        console.print(f"[yellow]Config file not found:[/yellow] {target}")
        raise SystemExit(0)

    # For JSON files, only remove the odoo-dev-mcp entry rather than deleting the file
    if target.suffix == ".json":
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            changed = False
            for key in ("mcpServers", "mcp.servers"):
                if key in data and "odoo-dev-mcp" in data[key]:
                    del data[key]["odoo-dev-mcp"]
                    changed = True
            if changed:
                target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                console.print(f"[green]✓[/green] Removed odoo-dev-mcp entry from [bold]{target}[/bold]")
            else:
                console.print(f"[yellow]No odoo-dev-mcp entry found in {target}[/yellow]")
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[red]Error reading {target}:[/red] {exc}")
            raise SystemExit(1)
    else:
        target.unlink()
        console.print(f"[green]✓[/green] Deleted [bold]{target}[/bold]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@main.command("status")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths.")
def cmd_status(addons_path: Optional[str]) -> None:
    """Show current project status."""
    import sqlite3

    config = _resolve_config(addons_path)
    db_path = config.db_path
    db_exists = db_path.exists()

    # Try to read metadata from the index
    schema_version = "—"
    last_indexed_str = "—"
    odoo_version = "—"

    if db_exists:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            def _get_meta(key: str) -> Optional[str]:
                row = conn.execute(
                    "SELECT value FROM index_meta WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None

            schema_version = _get_meta("schema_version") or "—"
            odoo_version = _get_meta("odoo_version_hint") or "—"
            indexed_at = _get_meta("indexed_at")
            if indexed_at:
                last_indexed_str = indexed_at
            conn.close()
        except Exception:
            pass

    index_size = _fmt_size(db_path) if db_exists else "not found"

    # Build addons path lines
    paths_text = ""
    for entry in config.addons_paths:
        p = entry.path.expanduser().resolve()
        exists_mark = "[green]✓[/green]" if p.exists() else "[red]✗[/red]"
        try:
            display = "./" + str(p.relative_to(config.root_path))
        except ValueError:
            display = str(p)
        label = f" ({entry.label})" if entry.label else ""
        paths_text += f"  {exists_mark} {display}{label}\n"

    panel_content = (
        f"[bold]Project:[/bold] {config.name}"
        + (f" — Odoo {odoo_version}" if odoo_version != "—" else "")
        + "\n"
        f"[bold]Root:[/bold] {config.root_path}\n"
        f"\n[bold]Addons paths:[/bold]\n{paths_text.rstrip()}\n"
        f"\n[bold]Index:[/bold] {db_path} ({index_size})\n"
        f"[bold]Schema version:[/bold] {schema_version}\n"
        f"[bold]Last indexed:[/bold] {last_indexed_str}\n"
        f"[bold]JS analysis:[/bold] {'enabled' if config.js_parsing else 'disabled'}\n"
        f"[bold]File watching:[/bold] {'enabled' if config.watch_enabled else 'disabled'}"
    )

    console.print(Panel(panel_content, title="OdooDevMCP Status", expand=False))

    if not db_exists:
        console.print(
            "\n[yellow]Index not built yet.[/yellow] Run [bold]odoo-dev-mcp index[/bold] to create it."
        )


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@main.command("stats")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths.")
def cmd_stats(addons_path: Optional[str]) -> None:
    """Show index statistics — row counts per table and index file size."""
    from .db.connection import get_conn

    config = _resolve_config(addons_path)
    db_path = config.db_path

    if not db_path.exists():
        console.print(
            f"[red]Index not found:[/red] {db_path}\n"
            "Run [bold]odoo-dev-mcp index[/bold] to build it first."
        )
        raise SystemExit(1)

    _TABLE_LABELS = [
        ("models", "Models"),
        ("fields", "Fields"),
        ("methods", "Methods"),
        ("views", "Views"),
        ("http_routes", "HTTP Routes"),
        ("actions", "Actions"),
        ("menus", "Menus"),
        ("cron_jobs", "Cron Jobs"),
        ("modules", "Modules"),
        ("access_rules", "Access Rules"),
        ("record_rules", "Record Rules"),
        ("js_components", "JS Components"),
        ("cross_refs", "Cross-References"),
    ]

    counts: dict[str, int] = {}
    with get_conn(db_path) as conn:
        for table, label in _TABLE_LABELS:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[label] = row[0] if row else 0
            except Exception:
                counts[label] = 0

    table = Table(title=f"Index Statistics — {config.name}", show_header=True, header_style="bold cyan")
    table.add_column("Table", style="dim")
    table.add_column("Rows", justify="right")

    for label, count in counts.items():
        table.add_row(label, f"{count:,}")

    console.print(table)
    console.print(f"\n[bold]Index size:[/bold] {_fmt_size(db_path)}")
    console.print(f"[bold]Location:[/bold] {db_path}")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@main.command("query")
@click.argument("tool_name")
@click.option("--model", default=None, help="Model name (e.g. sale.order).")
@click.option("--field", default=None, help="Field name.")
@click.option("--method", default=None, help="Method name.")
@click.option("--module", default=None, help="Module name.")
@click.option("--view-xml-id", "view_xml_id", default=None, help="View XML ID.")
@click.option("--button", default=None, help="Button name.")
@click.option("--path-prefix", "path_prefix", default=None, help="HTTP route path prefix.")
@click.option("--auth-filter", "auth_filter", default=None, help="HTTP auth filter (public/user/none).")
@click.option("--view-type", "view_type", default="form", show_default=True, help="View type for resolve_xml_view.")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths.")
@click.option("--pretty/--no-pretty", default=True, help="Pretty-print JSON output.")
def cmd_query(
    tool_name: str,
    model: Optional[str],
    field: Optional[str],
    method: Optional[str],
    module: Optional[str],
    view_xml_id: Optional[str],
    button: Optional[str],
    path_prefix: Optional[str],
    auth_filter: Optional[str],
    view_type: str,
    addons_path: Optional[str],
    pretty: bool,
) -> None:
    """Call a single MCP tool directly from the CLI and print JSON output.

    \b
    Examples:
      odoo-dev-mcp query get_model_schema --model sale.order
      odoo-dev-mcp query get_state_machine --model sale.order
      odoo-dev-mcp query get_http_routes --module sale
      odoo-dev-mcp query get_constraints --model stock.picking
    """
    from .tools.get_model_schema import get_model_schema
    from .tools.resolve_xml_view import resolve_xml_view
    from .tools.analyze_change_impact import analyze_change_impact
    from .tools.get_method_logic import get_method_logic
    from .tools.get_state_machine import get_state_machine
    from .tools.get_constraints import get_constraints
    from .tools.get_access_control import get_access_control
    from .tools.trace_compute_chain import trace_compute_chain
    from .tools.get_model_actions import get_model_actions
    from .tools.get_field_visibility import get_field_visibility
    from .tools.trace_button_to_method import trace_button_to_method
    from .tools.get_http_routes import get_http_routes

    config = _resolve_config(addons_path)
    db_path = config.db_path

    if not db_path.exists():
        console.print(
            f"[yellow]Warning:[/yellow] Index not found at {db_path}. "
            "The tool may return an error."
        )

    get_db = lambda: db_path  # noqa: E731

    _TOOLS = {
        "get_model_schema": lambda: get_model_schema(model, get_db),
        "resolve_xml_view": lambda: resolve_xml_view(model, view_type, get_db),
        "analyze_change_impact": lambda: analyze_change_impact(model, get_db, field, method),
        "get_method_logic": lambda: get_method_logic(model, method, get_db),
        "get_state_machine": lambda: get_state_machine(model, get_db),
        "get_constraints": lambda: get_constraints(model, get_db),
        "get_access_control": lambda: get_access_control(model, get_db),
        "trace_compute_chain": lambda: trace_compute_chain(model, field, get_db),
        "get_model_actions": lambda: get_model_actions(model, get_db),
        "get_field_visibility": lambda: get_field_visibility(model, field, get_db),
        "trace_button_to_method": lambda: trace_button_to_method(view_xml_id, button, get_db),
        "get_http_routes": lambda: get_http_routes(get_db, module, path_prefix, auth_filter),
    }

    if tool_name not in _TOOLS:
        available = "\n  ".join(sorted(_TOOLS.keys()))
        console.print(
            f"[red]Unknown tool:[/red] {tool_name}\n\nAvailable tools:\n  {available}"
        )
        raise SystemExit(1)

    try:
        result = asyncio.run(_TOOLS[tool_name]())
    except Exception as exc:
        console.print(f"[red]Tool error:[/red] {exc}")
        raise SystemExit(1)

    indent = 2 if pretty else None
    click.echo(json.dumps(result, indent=indent, default=str))


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

@main.command("graph")
@click.option("--model", "model_name", default=None, help="Odoo model name to centre on (e.g. sale.order).")
@click.option("--module", "module_name", default=None, help="Module name to centre on (e.g. sale).")
@click.option("--all", "all_models", is_flag=True, default=False,
              help="Full-codebase graph: all models with inheritance + field relations. Ignores --model/--module.")
@click.option("--depth", default=2, show_default=True, help="Hop depth for related models/modules (1–4).")
@click.option("--output", "output_path", default=None, help="Output HTML file path. Default: <name>_graph.html")
@click.option("--addons-path", "addons_path", default=None, help="Colon-separated addons paths.")
@click.option("--open", "open_browser", is_flag=True, default=False, help="Open the generated HTML in the browser.")
def cmd_graph(
    model_name: Optional[str],
    module_name: Optional[str],
    all_models: bool,
    depth: int,
    output_path: Optional[str],
    addons_path: Optional[str],
    open_browser: bool,
) -> None:
    """Generate an interactive HTML knowledge graph from the Odoo index.

    Shows ALL connections between components: model inheritance, relational
    fields, compute chains, views, state machines, security rules, cron jobs,
    actions, and module dependencies — with filter controls, zoom buttons,
    and PNG export.

    \b
    Examples:
      odoo-dev-mcp graph --model sale.order
      odoo-dev-mcp graph --model sale.order --depth 2 --open
      odoo-dev-mcp graph --module sale --depth 3
      odoo-dev-mcp graph --all --output full_graph.html
    """
    from .graph_html import generate_graph_html

    config = _resolve_config(addons_path)

    if not config.db_path.exists():
        console.print(
            f"[red]Index not found:[/red] {config.db_path}\n"
            "Run [bold]odoo-dev-mcp index[/bold] first."
        )
        raise SystemExit(1)

    if not all_models and not model_name and not module_name:
        console.print("[red]Error:[/red] Provide --model, --module, or --all.")
        raise SystemExit(1)

    if all_models:
        stem = "full_codebase"
    else:
        stem = model_name or module_name
    safe_stem = (stem or "graph").replace(".", "_").replace("/", "_")
    out_file = Path(output_path) if output_path else Path.cwd() / f"{safe_stem}_graph.html"

    target_label = "All models (full codebase)" if all_models else stem
    console.print(
        Panel(
            f"[bold]Target:[/bold] {target_label}\n"
            + (f"[bold]Depth:[/bold] {depth}\n" if not all_models else "")
            + f"[bold]Output:[/bold] {out_file}",
            title="OdooDevMCP — Knowledge Graph",
            expand=False,
        )
    )

    with console.status("[dim]Building graph…[/dim]"):
        try:
            html, title = generate_graph_html(
                db_path=config.db_path,
                model_name=model_name,
                module_name=module_name,
                depth=max(1, min(depth, 4)),
                all_models=all_models,
            )
        except ValueError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1)

    out_file.write_text(html, encoding="utf-8")

    console.print(f"[green]✓[/green] Saved [bold]{out_file}[/bold]  ({len(html)//1024}KB)")
    console.print(f"[dim]{title}[/dim]")

    if open_browser:
        import webbrowser
        webbrowser.open(out_file.as_uri())
        console.print("[dim]Opened in browser.[/dim]")
    else:
        console.print(f"\n[dim]Open with:[/dim]  open {out_file}")


# ---------------------------------------------------------------------------
# project sub-group
# ---------------------------------------------------------------------------

@main.group("project")
def project_group() -> None:
    """Manage multiple Odoo projects in the global registry."""


@project_group.command("add")
@click.option("--path", "project_path", required=True, help="Path to the project directory.")
@click.option("--name", "project_name", default=None, help="Project name (default: directory name).")
def project_add(project_path: str, project_name: Optional[str]) -> None:
    """Register a project in the global registry."""
    from .config import find_project_config, load_project_config, ProjectConfig, AddonsPathEntry
    from .registry import ProjectRegistry

    p = Path(project_path).expanduser().resolve()
    if not p.is_dir():
        console.print(f"[red]Error:[/red] '{p}' is not a directory.")
        raise SystemExit(1)

    config_toml = find_project_config(p)
    if config_toml:
        try:
            config = load_project_config(config_toml)
            if project_name:
                config.name = project_name
        except Exception as exc:
            console.print(f"[red]Error loading .odoo-dev-mcp.toml:[/red] {exc}")
            raise SystemExit(1)
    else:
        # Create minimal config
        name = project_name or p.name
        config = ProjectConfig(
            name=name,
            addons_paths=[AddonsPathEntry(path=p)],
            root_path=p,
        )
        console.print(
            f"[yellow]No .odoo-dev-mcp.toml found in {p}.[/yellow] "
            "Using directory as single addons path."
        )

    registry = ProjectRegistry()
    registry.add(config)
    console.print(f"[green]✓[/green] Registered project [bold]{config.name}[/bold] ({p})")


@project_group.command("list")
def project_list() -> None:
    """List all registered projects."""
    from .registry import ProjectRegistry

    registry = ProjectRegistry()
    entries = registry.list_all()

    if not entries:
        console.print("[dim]No projects registered. Use [bold]odoo-dev-mcp project add[/bold] to add one.[/dim]")
        return

    table = Table(title="Registered Projects", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Root Path", style="dim")
    table.add_column("Index", justify="center")
    table.add_column("Last Indexed", justify="right")
    table.add_column("Active", justify="center")

    for entry in entries:
        db_path = Path(entry.db_path)
        index_status = "[green]✓[/green]" if db_path.exists() else "[red]✗[/red]"
        last_idx = _fmt_ago(entry.last_indexed)
        active = "[green]●[/green]" if entry.is_active else ""

        table.add_row(
            entry.name,
            entry.root_path,
            index_status,
            last_idx,
            active,
        )

    console.print(table)


@project_group.command("info")
@click.argument("name")
def project_info(name: str) -> None:
    """Show detailed info for a registered project."""
    from .registry import ProjectRegistry

    registry = ProjectRegistry()
    entry = registry.get(name)

    if entry is None:
        console.print(f"[red]Project not found:[/red] {name}")
        raise SystemExit(1)

    db_path = Path(entry.db_path)
    db_size = _fmt_size(db_path) if db_path.exists() else "not built"

    paths_text = "\n".join(
        f"  [{i+1}] {ap['path']}" + (f" ({ap['label']})" if ap.get("label") else "")
        for i, ap in enumerate(entry.addons_paths)
    ) or "  (none)"

    created = (
        datetime.fromtimestamp(entry.created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if entry.created_at
        else "—"
    )
    last_idx = (
        datetime.fromtimestamp(entry.last_indexed, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if entry.last_indexed
        else "never"
    )

    content = (
        f"[bold]Name:[/bold] {entry.name}\n"
        f"[bold]Root:[/bold] {entry.root_path}\n"
        f"[bold]Config:[/bold] {entry.config_path or '(none)'}\n"
        f"[bold]Active:[/bold] {'yes' if entry.is_active else 'no'}\n"
        f"\n[bold]Addons paths:[/bold]\n{paths_text}\n"
        f"\n[bold]Index:[/bold] {entry.db_path} ({db_size})\n"
        f"[bold]Addons hash:[/bold] {entry.addons_hash}\n"
        f"[bold]Last indexed:[/bold] {last_idx}\n"
        f"[bold]Created:[/bold] {created}"
    )

    console.print(Panel(content, title=f"Project: {name}", expand=False))


@project_group.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def project_remove(name: str, yes: bool) -> None:
    """Remove a project from the registry (does not delete index files)."""
    from .registry import ProjectRegistry

    registry = ProjectRegistry()
    entry = registry.get(name)

    if entry is None:
        console.print(f"[red]Project not found:[/red] {name}")
        raise SystemExit(1)

    if not yes:
        confirmed = click.confirm(f"Remove project '{name}' from registry?", default=False)
        if not confirmed:
            console.print("[dim]Aborted.[/dim]")
            raise SystemExit(0)

    registry.remove(name)
    console.print(f"[green]✓[/green] Removed [bold]{name}[/bold] from registry.")
    console.print(
        f"[dim]Note: index file at {entry.db_path} was not deleted.[/dim]"
    )


@project_group.command("use")
@click.argument("name")
def project_use(name: str) -> None:
    """Mark a project as the active (default) project."""
    from .registry import ProjectRegistry

    registry = ProjectRegistry()
    entry = registry.get(name)

    if entry is None:
        console.print(f"[red]Project not found:[/red] {name}")
        raise SystemExit(1)

    registry.set_active(name)
    console.print(f"[green]✓[/green] [bold]{name}[/bold] is now the active project.")


# ---------------------------------------------------------------------------
# eval — benchmark suite
# ---------------------------------------------------------------------------

@main.group("eval")
def eval_group() -> None:
    """Benchmark OdooDevMCP accuracy, token efficiency, and performance."""


@eval_group.command("run")
@click.option(
    "--suite",
    default="all",
    type=click.Choice(["all", "accuracy", "tokens", "perf"]),
    show_default=True,
    help="Which benchmark suite to run.",
)
@click.option("--question", default=None, help="Run a single question by ID (e.g. FL-001).")
@click.option("--fixture", "fixture_path", default=None, type=click.Path(),
              help="Path to fixture addon directory (default: built-in fixture).")
@click.option("--output", "output_path", default=None, type=click.Path(),
              help="Directory to write reports into.")
@click.option(
    "--format", "fmt",
    default="markdown",
    type=click.Choice(["markdown", "json"]),
    show_default=True,
    help="Report format.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Print per-question results.")
def eval_run(suite: str, question: Optional[str], fixture_path: Optional[str],
             output_path: Optional[str], fmt: str, verbose: bool) -> None:
    """Run the OdooDevMCP benchmark suite."""
    import asyncio as _asyncio
    import sys as _sys
    from pathlib import Path as _Path

    # Locate the evaluate/ directory relative to this file
    eval_dir = _Path(__file__).parent.parent.parent / "evaluate"
    if not eval_dir.is_dir():
        console.print(f"[red]evaluate/ directory not found at:[/red] {eval_dir}")
        console.print("[dim]Run this command from the OdooDevMCP repository root.[/dim]")
        raise SystemExit(1)

    # Insert evaluate/ into sys.path so runner.py / grader.py / token_counter.py resolve
    if str(eval_dir) not in _sys.path:
        _sys.path.insert(0, str(eval_dir))

    import importlib
    runner = importlib.import_module("runner")

    # Override paths if provided
    if fixture_path:
        runner.FIXTURE_ROOT = _Path(fixture_path).resolve()
    if output_path:
        runner.REPORTS_DIR = _Path(output_path).resolve()

    _asyncio.run(runner.main(suite, question, verbose))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
