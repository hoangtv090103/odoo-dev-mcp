# OdooDevMCP

**A Model Context Protocol (MCP) server that gives AI coding assistants a deep, structured understanding of any Odoo codebase.**

OdooDevMCP builds a local SQLite knowledge graph by statically analysing your Odoo source tree. Once indexed, 18 MCP tools let the AI navigate models, fields, state machines, views, security rules, HTTP routes, and cross-module relationships — without ever reading source files at runtime.

Built on [FastMCP](https://github.com/jlowin/fastmcp) · Python 3.10+ · SQLite · tree-sitter · lxml

> **Inspired by** [code-review-graph](https://github.com/tirth8205/code-review-graph) by [tirth8205](https://github.com/tirth8205) — a project that builds a persistent structural map of any codebase using Tree-sitter so AI assistants read only what matters (up to 49× fewer tokens). OdooDevMCP applies the same core idea to the Odoo ecosystem, extending it with Odoo-specific semantics: model inheritance chains, XML view resolution, state machines, security ACLs, and 7-phase incremental indexing across Python, XML, CSV, and JavaScript.

---

## Table of Contents

- [Why OdooDevMCP?](#why-odoodevmcp)
- [Architecture Overview](#architecture-overview)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [MCP Tools Reference](#mcp-tools-reference)
- [MCP Prompts](#mcp-prompts)
- [Agent Skill Auto-activation](#agent-skill-auto-activation)
- [Multi-Project Support](#multi-project-support)
- [Knowledge Graph Schema](#knowledge-graph-schema)
- [Indexing Pipeline](#indexing-pipeline)
- [IDE Integration](#ide-integration)
- [Contributing](#contributing)

---

## Why OdooDevMCP?

Odoo is one of the most complex open-source frameworks in existence. A typical enterprise installation has hundreds of custom modules, thousands of models, and inter-dependencies that span Python, XML, CSV, and JavaScript — all layered through Odoo's class inheritance, view inheritance, and data inheritance systems.

When an AI assistant tries to help with Odoo development **without** this tool, the result is predictably poor:

```
Problem                               Consequence for AI
────────────────────────────────────  ─────────────────────────────────────────
Model fields come from 5 parent       AI only sees the leaf class, misses
classes across 3 modules              inherited fields and overrides

State machine defined in XML data     AI can't find it via Python search;
files, not Python code                guesses or hallucinates transitions

View inherits from 4 different        AI can't reconstruct the final rendered
modules via xpath                     form; suggests edits to wrong template

ACL + record rules + sudo() calls     AI gives security advice that silently
spread across 8 files                 breaks record-level access

Cross-module compute chain:           AI suggests fixes that break the chain
a.field → b.field → c.field          midway through
```

OdooDevMCP solves this by running a **one-time static analysis** of the entire codebase and materialising everything into a structured, queryable SQLite knowledge graph. The AI then works from precision queries — not grepping raw files.

### Key advantages

**1. Odoo-aware, not just Python-aware**
The indexer understands `_inherit`, `_inherits`, XML `<inherit>` / `<xpath>`, `ir.model.access`, `ir.rule`, `@api.depends`, `@api.onchange`, `_sql_constraints`, and Odoo's ORM conventions natively. Generic code-search tools do not.

**2. Cross-layer relationships are pre-resolved**
When you ask "what fields does `sale.order` have?", the answer includes fields inherited from `mail.thread`, `portal.mixin`, and any custom `sale.order` extensions across your custom modules — resolved at index time, not at query time.

**3. Zero runtime I/O per query**
Once indexed, the AI issues structured SQL queries against the knowledge graph. No source files are read, no greps are launched. A `get_model_schema()` call returns a complete model snapshot in milliseconds.

**4. Incremental — stays fresh with low overhead**
Only modules whose files have changed are re-indexed. A typical incremental run after editing one module takes 2–5 seconds, not minutes.

**5. Atomic index writes — no corrupt state**
Full rebuilds write to a temp file and rename atomically on success. If the process dies mid-build, the previous index remains intact.

**6. Agent Skill auto-activation**
After `odoo-dev-mcp install`, an Agent Skill file is written to `.claude/skills/` and `.agents/skills/`. Claude Code, Cursor, and VS Code Copilot automatically activate the skill — and reach for `get_project_context()` first — without requiring any manual prompt engineering.

```
Without OdooDevMCP                   With OdooDevMCP
─────────────────────────────────    ─────────────────────────────────
AI reads 50+ files for one field     get_model_schema() → one call
AI misses inherited methods          Inheritance chain pre-resolved
AI guesses state transitions         get_state_machine() → full graph
AI ignores security rules            get_access_control() → all ACLs
AI breaks compute chain on edit      analyze_change_impact() warns upfront
AI picks wrong tool (guesses)        Agent Skill auto-activates correct MCP
```

### Benchmark

Measured against a purpose-built Odoo fixture (4 models, 6-state machine, multi-level inheritance, ACL + record rules). Run `odoo-dev-mcp eval` to reproduce.

| Metric | Result |
|--------|--------|
| Overall index accuracy | **1.00** (58/58 questions) |
| All categories | **1.00** — field lookup, state machines, security, compute chains, transitive inheritance, method overrides |
| Token reduction vs reading files | **2.2× average**, up to **8.4×** for change-impact queries |
| Compute / method / change-impact queries | **6–8× token reduction** |
| Full index build time | **14 ms** |
| Incremental update (1 module changed) | **3 ms** |

Token reduction scales with codebase size — on a real enterprise Odoo installation with hundreds of modules, the AI would otherwise search through thousands of files to answer the same questions.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         AI Coding Assistant                          │
│              (Claude Code · Cursor · VS Code Copilot)               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  MCP Protocol (stdio)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        OdooDevMCP Server                             │
│                                                                      │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │                     18 MCP Tools                           │    │
│   │  get_project_context · get_model_schema · get_state_machine│    │
│   │  analyze_change_impact · get_access_control · trace_path   │    │
│   │  resolve_xml_view · search_odoo_entities · ...             │    │
│   └───────────────────────────┬────────────────────────────────┘    │
│                               │                                      │
│   ┌───────────────────────────▼────────────────────────────────┐    │
│   │               SQLite Knowledge Graph                        │    │
│   │  modules · models · fields · methods · views · http_routes  │    │
│   │  state_machines · access_rules · record_rules · cron_jobs   │    │
│   │  actions · menus · js_components · decorators_detail · ...  │    │
│   └───────────────────────────▲────────────────────────────────┘    │
│                               │                                      │
│   ┌───────────────────────────┴────────────────────────────────┐    │
│   │                   Indexing Pipeline                         │    │
│   │  Phase 1: Structural  →  Phase 2: Decorators               │    │
│   │  Phase 3: Behavioral  →  Phase 4: Cross-layer XML           │    │
│   │  Phase 5: Security    →  Phase 6: JavaScript                │    │
│   │  Phase 7: Cross-references & FTS5                           │    │
│   └───────────────────────────▲────────────────────────────────┘    │
└───────────────────────────────│─────────────────────────────────────┘
                                │
            ┌───────────────────┴───────────────────┐
            │           Odoo Source Tree             │
            │  *.py  *.xml  *.csv  *.js              │
            │  custom addons · community modules     │
            └───────────────────────────────────────┘
```

---

## How It Works

### 1. Index (one-time, ~1–2 min for large codebases)

```bash
odoo-dev-mcp index
```

The indexer scans every `.py`, `.xml`, `.csv`, and `.js` file across your addons paths. It runs 7 sequential phases, each building on the previous, and stores results in a SQLite database at `<project_root>/.odoo-dev-mcp/index.db`.

**Atomic write guarantee:** Full rebuilds write to a temporary `index.db.new` file and only rename it into place when every phase succeeds. If the process is killed mid-index, the previous good index is left intact.

**Incremental updates:** After the initial build, subsequent runs only re-index modules whose files have changed (xxHash-based file fingerprinting). A typical incremental update on a changed module takes seconds.

### 2. Serve

```bash
odoo-dev-mcp serve
```

Starts a stdio-transport MCP server. Your IDE connects to it and the AI can now call the 18 tools to query the knowledge graph directly.

### 3. Query

The AI calls `get_project_context()` first to get an orientation map, then uses targeted tools to explore exactly what it needs. No source files are read at query time.

---

## Requirements

- Python 3.10+
- Odoo source tree (any version; version is auto-detected from `release.py` or `__manifest__.py`)
- One of: Claude Code, Cursor, VS Code with GitHub Copilot, or any MCP-compatible agent

---

## Installation

```bash
git clone https://github.com/your-org/odoo-dev-mcp
cd odoo-dev-mcp
pip install -e .
```

**Optional extras:**

```bash
pip install xxhash     # faster hashing (recommended)
pip install tomli      # TOML support for Python 3.10 only
```

---

## Quick Start

```bash
# 1. Go to your Odoo project root
cd /path/to/your/odoo_project

# 2. Create a project config
odoo-dev-mcp init

# 3. Build the knowledge graph
odoo-dev-mcp index

# 4. Install MCP config + Agent Skill for your IDE
odoo-dev-mcp install --platform claude-code   # or: cursor / vscode

# 5. Open your IDE — the MCP server starts automatically
```

After step 4, the following files are created:

| File | Purpose |
|---|---|
| `.mcp.json` | MCP server config (Claude Code) |
| `.claude/skills/odoo-dev-mcp/SKILL.md` | Agent Skill for Claude Code |
| `.agents/skills/odoo-dev-mcp/SKILL.md` | Agent Skill for Cursor / VS Code Copilot |

The Agent Skill enables **automatic activation** — the AI detects Odoo-related questions and calls `get_project_context()` without any manual prompting.

---

## Configuration

### Project config: `.odoo-dev-mcp.toml`

Created by `odoo-dev-mcp init` and placed in your project root. Commit this file to version control.

```toml
# OdooDevMCP project configuration
# The knowledge-graph index is stored in .odoo-dev-mcp/index.db
# Add   .odoo-dev-mcp/   to your .gitignore

[project]
name = "my_odoo_project"

[[addons_path]]
path = "./custom_addons"
label = "custom"

[[addons_path]]
path = "/opt/odoo/community/addons"
label = "community"

[[addons_path]]
path = "/opt/odoo/odoo/addons"
label = "core"

[index]
exclude_patterns = ["*/test_*", "*/demo_data"]
js_parsing = false          # set to true to also index OWL/JS components

[watch]
enabled = true              # auto-rebuild index when files change
debounce_ms = 250
```

### Global config: `~/.config/odoo-dev-mcp/config.toml`

```toml
[index]
fts_detail = "column"       # FTS5 detail level: column / row / none
js_parsing = false
workers = 0                 # 0 = auto-detect

[watch]
enabled = true
debounce_ms = 250
```

### File layout after setup

```
your_project/
├── .odoo-dev-mcp.toml              ← project config  (commit to git)
├── .mcp.json                       ← MCP config for Claude Code
├── .cursor/mcp.json                ← MCP config for Cursor
├── .vscode/settings.json           ← MCP config for VS Code
├── .claude/
│   └── skills/odoo-dev-mcp/
│       └── SKILL.md                ← Agent Skill for Claude Code
├── .agents/
│   └── skills/odoo-dev-mcp/
│       └── SKILL.md                ← Agent Skill for Cursor / Copilot
└── .odoo-dev-mcp/                  ← add to .gitignore
    ├── index.db                    ← knowledge graph (SQLite)
    └── INDEX_INFO.json             ← lightweight status / stats file
```

---

## CLI Reference

### `odoo-dev-mcp init`

Interactive setup — creates `.odoo-dev-mcp.toml` in the current directory and adds `.odoo-dev-mcp/` to `.gitignore`.

```bash
odoo-dev-mcp init
odoo-dev-mcp init --name "my_project" --yes   # non-interactive defaults
```

---

### `odoo-dev-mcp index`

Build or rebuild the knowledge graph index.

```bash
odoo-dev-mcp index                    # incremental if index already exists
odoo-dev-mcp index --force            # full rebuild from scratch
odoo-dev-mcp index -f                 # shorthand
odoo-dev-mcp index --incremental      # force incremental mode
odoo-dev-mcp index --addons-path /path/to/addons:/path/to/more
```

---

### `odoo-dev-mcp serve`

Start the MCP server over stdio. This is what your IDE's MCP config invokes.

```bash
odoo-dev-mcp serve
odoo-dev-mcp serve --addons-path /path/to/addons
```

If no complete index exists when the server starts, it automatically runs `index` in a background thread and streams progress to stderr.

---

### `odoo-dev-mcp install`

Write IDE/MCP config files and Agent Skill files for the current project.

```bash
odoo-dev-mcp install --platform claude-code
odoo-dev-mcp install --platform cursor
odoo-dev-mcp install --platform vscode
odoo-dev-mcp install --platform claude-code --force   # overwrite existing files
```

---

### `odoo-dev-mcp status`

Show index freshness, module count, and stale module list.

```bash
odoo-dev-mcp status
```

---

### `odoo-dev-mcp stats`

Show row counts per table in the knowledge graph.

```bash
odoo-dev-mcp stats
```

---

### `odoo-dev-mcp graph`

Generate an interactive HTML knowledge graph and open it in the browser.

```bash
odoo-dev-mcp graph
odoo-dev-mcp graph --model sale.order
odoo-dev-mcp graph --module sale --output graph.html
```

---

### `odoo-dev-mcp query`

Call any MCP tool directly from the CLI and print JSON output — useful for scripting and debugging.

```bash
odoo-dev-mcp query get_project_context
odoo-dev-mcp query get_model_schema --args '{"model_name": "sale.order"}'
odoo-dev-mcp query search_odoo_entities --args '{"query": "invoice", "types": ["model"]}'
```

---

### `odoo-dev-mcp project`

Manage multiple Odoo projects in the global registry.

```bash
odoo-dev-mcp project list
odoo-dev-mcp project add --name erp_v17 --path /projects/erp_v17
odoo-dev-mcp project info erp_v17
odoo-dev-mcp project use erp_v17
odoo-dev-mcp project remove erp_v17
```

---

## MCP Tools Reference

> **Always call `get_project_context()` first** in a session. It returns index health, top models, top modules, and a suggested tool chain in under 400 tokens.

### Entry Point & Index Management

| Tool | Description |
|---|---|
| `get_project_context(focus_model?)` | Compact index overview + optional per-model quick facts and suggested next tools |
| `get_index_status()` | Index health, staleness check, row counts per table |
| `build_index(reset?, incremental?)` | Trigger a rebuild from the AI side |

### Model Exploration

| Tool | Description |
|---|---|
| `get_model_schema(model, compact?, fields_limit?)` | All fields with types, compute methods, related paths, and the full inheritance chain |
| `get_state_machine(model)` | All states, transitions, trigger methods, and guard conditions |
| `get_method_logic(model, method)` | Decorators, state transitions caused, ORM calls, `sudo()` usage, return value |
| `get_model_actions(model)` | Window actions, server actions, cron jobs, reports, and menu items |
| `get_constraints(model)` | `@api.constrains`, SQL `_sql_constraints`, and `@api.onchange` validators |

### View & UI Layer

| Tool | Description |
|---|---|
| `resolve_xml_view(model, view_type)` | Fully merged XML view after applying all `inherit_id` overrides |
| `get_field_visibility(model, field)` | Every view where the field appears and any `attrs` rules (invisible / required / readonly) |
| `trace_button_to_method(view_xml_id, button_name)` | Button → action / method → Python code chain |

### Security

| Tool | Description |
|---|---|
| `get_access_control(model)` | Model-level ACLs (CRUD per group), record rules with domain filters, field-level `groups=` |

### HTTP Controllers

| Tool | Description |
|---|---|
| `get_http_routes(module?, auth_type?)` | All `@http.route` endpoints with URL pattern, method, auth type, and CORS setting |

### Graph Traversal & Impact Analysis

| Tool | Description |
|---|---|
| `trace_odoo_path(model, depth?, edge_types?)` | BFS walk from a model following field_rel, inherit, compute, state, and action edges |
| `trace_compute_chain(model, field)` | Full `@api.depends` dependency chain for a computed field |
| `analyze_change_impact(model, field_name?)` | Blast radius: views, methods, models, and compute chains affected by a change |

### Search

| Tool | Description |
|---|---|
| `search_odoo_entities(query, types?, module?, limit?)` | Full-text + keyword search across models, fields, methods, views, and routes |

### Visualisation

| Tool | Description |
|---|---|
| `get_model_graph(model?, module?, depth?)` | Returns a Mermaid diagram of model relationships |

---

## MCP Prompts

Prompts are guided workflow templates that prime the AI with a step-by-step tool-call sequence for common Odoo development tasks.

| Prompt | Arguments | When to use |
|---|---|---|
| `analyze_odoo_model` | `model_name` | Full deep-dive: fields, methods, state machine, security, views, dependencies |
| `debug_field_issue` | `model_name`, `field_name` | Diagnose a wrong or missing field value — traces compute chain, onchange, visibility |
| `plan_model_change` | `model_name`, `change_description` | Pre-change impact assessment — blast radius, dependencies, security implications |
| `understand_business_flow` | `model_name`, `flow_description?` | End-to-end lifecycle trace from record creation through state transitions to completion |
| `security_review` | `model_name`, `security_concern?` | ACL audit, record rules, field-level groups, HTTP exposure, unsafe `sudo()` usage |

---

## Agent Skill Auto-activation

OdooDevMCP follows the [Agent Skills](https://agentskills.io) open standard. Running `odoo-dev-mcp install` writes a `SKILL.md` to both:

- `.claude/skills/odoo-dev-mcp/SKILL.md` — Claude Code
- `.agents/skills/odoo-dev-mcp/SKILL.md` — VS Code Copilot, Cursor, OpenAI Codex, and any [Agent Skills-compatible](https://agentskills.io) tool

Skills use **progressive disclosure**: the AI loads only the `name` and `description` at startup (negligible cost), and only reads the full body when a task matches.

```
description: >
  Use whenever the user asks about this Odoo codebase: model structure,
  fields, methods, business flows, state machines, security rules, HTTP
  controllers, XML views, compute chains, or change impact...
```

When activated, the skill instructs the AI to call `get_project_context()` first — solving the core problem of the AI not knowing which tools to use for Odoo-specific questions.

```
User asks: "How does the sales workflow handle quotation confirmation?"
              │
              ▼
Agent Skills description matches → skill body loaded into context
              │
              ▼
AI calls get_project_context(focus_model="sale.order")
              │
              ▼
AI follows suggested chain: get_state_machine → get_method_logic → ...
```

---

## Multi-Project Support

OdooDevMCP maintains a global project registry, making it easy to switch between multiple Odoo codebases.

```bash
# Register projects
odoo-dev-mcp project add --name erp_v17 --path /projects/erp_v17
odoo-dev-mcp project add --name erp_v16 --path /projects/erp_v16

# List all registered projects
odoo-dev-mcp project list

# Switch active project
odoo-dev-mcp project use erp_v17

# Serve all projects simultaneously (multi-tenant MCP server)
odoo-dev-mcp serve --all-projects
```

Each project has its own `index.db` at `<project_root>/.odoo-dev-mcp/index.db`. When serving multiple projects, each is exposed as a separate MCP tool namespace.

---

## Knowledge Graph Schema

The index is a SQLite database with 23 tables and 26 indexes (including 3 partial indexes for BFS traversal performance).

```
Core Structure
  modules          name, path, version, author, depends (JSON)
  models           name, module, description, inherit_type, abstract, transient
  fields           name, type, model, compute, related, comodel, required, ...
  methods          name, model, decorators, state_transitions, body_text

UI Layer
  views            xml_id, model, type, arch, priority, module
  actions          xml_id, name, model, type (window / server / report / client)
  menus            xml_id, name, action_id, parent_id, sequence
  qweb_templates   xml_id, module, content
  email_templates  xml_id, model, subject, module

Security Layer
  access_rules     model, group, perm_read, perm_write, perm_create, perm_unlink
  record_rules     model, group, domain_force, permissions
  field_groups_map model, field, groups

Behaviour & Logic
  state_machines      model, field_name, states (JSON), transitions (JSON)
  decorators_detail   method, type, depends, constrains, onchange, route info
  http_routes         url, method, auth, csrf, cors, module, controller
  cron_jobs           name, model, method, interval_type, interval_number

Cross-References
  module_deps           module → dependency edges
  related_field_paths   resolved dotted-path chains for `related=` fields
  view_element_refs     field / button references inside views
  context_dependencies  @api.depends_context keys per method
  selection_extensions  _selection_add values added by inheriting modules
  js_components         OWL component name, module, props

Infrastructure
  file_hashes    module_name → xxh3-64 hash (powers incremental indexing)
  index_meta     key-value: indexed_at, schema_version, odoo_version_hint, ...
```

---

## Indexing Pipeline

```
Odoo Source Tree (.py / .xml / .csv / .js)
          │
          ▼
Phase 0 ─ Module Scanner
          Discovers all Odoo modules (__manifest__.py)
          Builds ModuleRecord list with paths and metadata
          │
          ▼
Phase 1 ─ Structural  (tree-sitter Python AST)
          → modules, models (primary + _inherit), fields, methods
          │
          ▼
Phase 2 ─ Decorators
          Enriches methods: @api.depends, @api.constrains,
          @api.onchange, @http.route details
          │
          ▼
Phase 3 ─ Behavioral  (regex on method bodies)
          → state transitions, ORM calls, sudo() usage
          → state_machines table
          │
          ▼
Phase 4 ─ Cross-layer XML  (lxml)
          → views, actions, menus, cron_jobs,
            qweb_templates, email_templates,
            view_element_refs
          │
          ▼
Phase 5 ─ Security
          CSV ir.model.access → access_rules
          XML ir.rule records → record_rules
          field groups_map
          │
          ▼
Phase 6 ─ JavaScript  (optional, js_parsing = true)
          → js_components (OWL components)
          │
          ▼
Phase 7 ─ Cross-references & FTS5
          → related_field_paths resolved
          → context_dependencies, selection_extensions
          → FTS5 full-text search index built
          │
          ▼
     index.db.new
          │  (atomic os.replace — only on full success)
          ▼
     index.db  +  INDEX_INFO.json
```

### Incremental index flow

```
odoo-dev-mcp index   (when index already exists)
          │
          ▼
    Scan all module directories
          │
          ▼
    Compare xxHash fingerprints against stored hashes
          │
          ├── unchanged modules ──→ skip (zero work)
          │
          └── changed / new modules ──→ delete old rows
                                        re-run phases 1–7
                                        update file_hashes
```

---

## IDE Integration

### Claude Code

```bash
odoo-dev-mcp install --platform claude-code
```

Creates `.mcp.json` in the project root. Claude Code picks it up automatically on the next session start.

### Cursor

```bash
odoo-dev-mcp install --platform cursor
```

Creates `.cursor/mcp.json`.

### VS Code with GitHub Copilot

```bash
odoo-dev-mcp install --platform vscode
```

Merges the MCP server entry into `.vscode/settings.json`.

### Manual MCP configuration

```json
{
  "mcpServers": {
    "odoo-dev-mcp": {
      "command": "odoo-dev-mcp",
      "args": ["serve"],
      "type": "stdio"
    }
  }
}
```

With explicit addons paths (no `.odoo-dev-mcp.toml` needed):

```json
{
  "mcpServers": {
    "odoo-dev-mcp": {
      "command": "odoo-dev-mcp",
      "args": [
        "serve",
        "--addons-path",
        "/path/to/custom_addons:/path/to/odoo/addons"
      ],
      "type": "stdio"
    }
  }
}
```

---

## Contributing

### Project structure

```
odoo-dev-mcp/
├── src/odoo_dev_mcp/
│   ├── cli.py                    # Click CLI (init / index / serve / install / ...)
│   ├── config.py                 # Config loading (.toml) and resolution
│   ├── prompts.py                # 5 guided workflow MCP prompts
│   ├── registry.py               # Global project registry (SQLite)
│   ├── version_detect.py         # Odoo version auto-detection
│   ├── graph_html.py             # Interactive HTML graph generator (vis.js)
│   ├── db/
│   │   ├── schema.py             # CREATE TABLE / CREATE INDEX DDL (23 tables, 26 indexes)
│   │   └── connection.py         # SQLite open / async query helpers
│   ├── indexer/
│   │   ├── pipeline.py           # Orchestrator: runs phases 1–7, atomic write
│   │   ├── module_scanner.py     # Module discovery (__manifest__.py)
│   │   ├── structural.py         # Phase 1: models, fields, methods
│   │   ├── decorators.py         # Phase 2: decorator detail
│   │   ├── behavioral.py         # Phase 3: method body analysis
│   │   ├── crosslayer.py         # Phase 4: XML views / actions / menus
│   │   ├── security.py           # Phase 5: ACL / record rules
│   │   ├── js.py                 # Phase 6: JS/OWL components
│   │   ├── crossref.py           # Phase 7: FTS5 + cross-references
│   │   └── hashing.py            # xxHash module fingerprinting
│   ├── parsers/
│   │   ├── python_parser.py      # tree-sitter Python AST parser
│   │   ├── xml_parser.py         # lxml XML parser
│   │   └── js_parser.py          # Regex-based JS parser
│   ├── tools/                    # 18 MCP tool implementations
│   │   ├── get_project_context.py
│   │   ├── get_model_schema.py
│   │   ├── get_state_machine.py
│   │   ├── analyze_change_impact.py
│   │   ├── get_access_control.py
│   │   ├── trace_path.py
│   │   ├── search_entities.py
│   │   └── ...
│   └── server/
│       ├── single.py             # Single-project FastMCP server
│       └── multi.py              # Multi-project FastMCP server
└── tests/
```

### Running tests

```bash
pip install -e ".[dev]"
pytest
pytest -x -v tests/test_pipeline.py    # single file
```

### Adding a new MCP tool

1. Create `src/odoo_dev_mcp/tools/my_tool.py` with an `async def my_tool(...)` function
2. Register it in `src/odoo_dev_mcp/tools/registry.py`
3. The tool is automatically available in both single and multi-project server modes

### Code style

```bash
ruff check src/
ruff format src/
```

---

## Acknowledgments

OdooDevMCP was inspired by **[code-review-graph](https://github.com/tirth8205/code-review-graph)** by [tirth8205](https://github.com/tirth8205) — a project that builds a persistent structural map of any codebase using Tree-sitter so AI assistants read only what matters, achieving up to 49× fewer tokens on daily coding tasks.

That core insight — *build the graph once, query it many times* — is exactly what OdooDevMCP brings to the Odoo ecosystem. We extended it with:

- Odoo-specific semantics (`_inherit`, `_inherits`, `ir.rule`, `ir.model.access`, XML xpath inheritance, `@api.depends` chains)
- A 7-phase incremental indexing pipeline across Python, XML, CSV, and JavaScript
- 18 MCP tools covering the full Odoo development lifecycle
- Agent Skill auto-activation so the AI reaches for the right tool automatically

Thank you to [tirth8205](https://github.com/tirth8205) for the original inspiration.

---

## License

MIT — see [LICENSE](LICENSE) for details.
