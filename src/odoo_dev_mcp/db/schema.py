"""
SQLite schema for Odoo Dev MCP.

All 16 tables + FTS5 virtual tables.
"""

from __future__ import annotations

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ── Core structural tables ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS modules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    path            TEXT NOT NULL,
    version         TEXT,
    category        TEXT,
    depends         TEXT DEFAULT '[]',   -- JSON: list of module names
    auto_install    INTEGER DEFAULT 0,
    installable     INTEGER DEFAULT 1,
    application     INTEGER DEFAULT 0,
    summary         TEXT,
    description     TEXT,
    author          TEXT,
    website         TEXT
);

CREATE TABLE IF NOT EXISTS models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,       -- 'sale.order'
    python_class    TEXT,               -- 'SaleOrder'
    inherit_type    TEXT CHECK(inherit_type IN ('primary','_inherit','_inherits')) DEFAULT 'primary',
    inherit_model   TEXT,               -- for _inherit
    inherits_map    TEXT DEFAULT '{}',  -- JSON: {field: model} for _inherits
    description     TEXT,               -- _description
    table_name      TEXT,               -- _table override
    rec_name        TEXT,               -- _rec_name
    order_field     TEXT,               -- _order
    abstract        INTEGER DEFAULT 0,
    transient       INTEGER DEFAULT 0,
    log_access      INTEGER DEFAULT 1,
    module_name     TEXT,
    file_path       TEXT,
    line_number     INTEGER,
    UNIQUE(name, module_name)
);
CREATE INDEX IF NOT EXISTS idx_models_name ON models(name);
CREATE INDEX IF NOT EXISTS idx_models_module ON models(module_name);

CREATE TABLE IF NOT EXISTS fields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    field_type      TEXT NOT NULL,      -- 'Char', 'Many2one', etc.
    comodel_name    TEXT,               -- for relational fields
    string_label    TEXT,
    required        INTEGER DEFAULT 0,
    readonly        INTEGER DEFAULT 0,
    store           INTEGER DEFAULT 1,
    index_field     INTEGER DEFAULT 0,
    compute         TEXT,               -- method name
    inverse         TEXT,               -- inverse method name
    search          TEXT,               -- search method name
    related         TEXT,               -- 'partner_id.name'
    depends         TEXT DEFAULT '[]',  -- JSON: @api.depends fields
    default_val     TEXT,
    help_text       TEXT,
    copy_field      INTEGER DEFAULT 1,
    tracking        INTEGER DEFAULT 0,
   
    groups          TEXT,               -- 'sales_team.group_sale_salesman'
    states_visibility TEXT DEFAULT '{}', -- JSON
    selection_values TEXT DEFAULT '[]', -- JSON: [['key','Label'],...]
    selection_add   TEXT DEFAULT '[]',  -- JSON: extension values
    ondelete_behavior TEXT,             -- cascade/set null/restrict
    domain_expr     TEXT,
    delegate        INTEGER DEFAULT 0,
    currency_field  TEXT,
    digits          TEXT,               -- JSON: [precision, scale]
    -- Location
    module_name     TEXT,
    file_path       TEXT,
    line_number     INTEGER,
    UNIQUE(model_name, field_name, module_name)
);
CREATE INDEX IF NOT EXISTS idx_fields_model ON fields(model_name);
CREATE INDEX IF NOT EXISTS idx_fields_comodel ON fields(comodel_name);
CREATE INDEX IF NOT EXISTS idx_fields_compute ON fields(compute);
-- BFS graph traversal: "give me all relational edges OUT of model X"
-- Used by trace_path for field_rel edge type.
-- Covers: WHERE model_name=? AND field_type IN ('Many2one','One2many','Many2many')
--         AND comodel_name IS NOT NULL
-- Without this, every BFS hop does a full scan of the fields table filtered by type.
CREATE INDEX IF NOT EXISTS idx_fields_relational
    ON fields(model_name, field_type, comodel_name)
    WHERE comodel_name IS NOT NULL;
-- trace_compute_chain: "which fields on model X are computed, and by which method?"
-- Covers: WHERE model_name=? AND compute IS NOT NULL
-- Also used by tool_01 compact mode to flag computed fields quickly.
CREATE INDEX IF NOT EXISTS idx_fields_computed
    ON fields(model_name, compute)
    WHERE compute IS NOT NULL;

CREATE TABLE IF NOT EXISTS methods (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    method_name     TEXT NOT NULL,
    decorator_types TEXT DEFAULT '[]',  -- JSON
    api_returns_model TEXT,
    is_cron_target  INTEGER DEFAULT 0,
    ormcache_keys   TEXT,               -- JSON
    -- Body analysis
    calls_models    TEXT DEFAULT '[]',  -- JSON: ORM calls to other models
    state_transitions TEXT DEFAULT '[]',-- JSON: [(from, to, field), ...]
    raises_validation INTEGER DEFAULT 0,
    -- Location
    module_name     TEXT,
    file_path       TEXT,
    line_number     INTEGER,
    body_start_line INTEGER,
    body_end_line   INTEGER,
    UNIQUE(model_name, method_name, module_name)
);
CREATE INDEX IF NOT EXISTS idx_methods_model ON methods(model_name);
-- get_state_machine + understand_business_flow: "does model X have state transitions?"
-- Covers: WHERE model_name=? AND state_transitions != '[]'
-- Avoids scanning all methods when looking for state-changing methods on a model.
CREATE INDEX IF NOT EXISTS idx_methods_state_transitions
    ON methods(model_name, state_transitions)
    WHERE state_transitions != '[]';

CREATE TABLE IF NOT EXISTS views (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT,
    model           TEXT,
    view_type       TEXT,               -- 'form','list','kanban',etc.
    inherit_id      TEXT,               -- parent view xml_id
    priority        INTEGER DEFAULT 16,
    arch_summary    TEXT,               -- top-level field names JSON
    field_names     TEXT DEFAULT '[]',  -- JSON: all field names referenced
    button_names    TEXT DEFAULT '[]',  -- JSON: all button names referenced
   
    view_group      TEXT,               -- groups attr on root element
    view_attrs      TEXT DEFAULT '{}',  -- JSON: element visibility summary
    -- Location
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_views_model ON views(model);
CREATE INDEX IF NOT EXISTS idx_views_xml_id ON views(xml_id);

CREATE TABLE IF NOT EXISTS module_deps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    module_name     TEXT NOT NULL,
    depends_on      TEXT NOT NULL,
    UNIQUE(module_name, depends_on)
);

-- ── Decorator detail table ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS decorators_detail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT,
    method_name     TEXT,
    decorator_type  TEXT NOT NULL,
    -- Decorator-specific data
    depends_fields      TEXT,           -- JSON
    depends_ctx_keys    TEXT,           -- JSON
    constrains_fields   TEXT,           -- JSON
    onchange_fields     TEXT,           -- JSON
    ondelete_at_unlink  INTEGER,
    returns_model       TEXT,
    ormcache_keys       TEXT,           -- JSON
    http_route          TEXT,
    http_auth           TEXT,
    http_type           TEXT,
    http_methods        TEXT,           -- JSON
    test_tags           TEXT,           -- JSON
    -- Location
    file_path   TEXT,
    line_number INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dec_model_method ON decorators_detail(model_name, method_name);
CREATE INDEX IF NOT EXISTS idx_dec_type ON decorators_detail(decorator_type);

-- ── HTTP routes table ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS http_routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    route_pattern   TEXT NOT NULL,
    route_patterns  TEXT,               -- JSON list
    auth            TEXT,
    route_type      TEXT,               -- 'http','json'
    http_methods    TEXT DEFAULT '["GET","POST"]',
    website         INTEGER DEFAULT 0,
    sitemap         INTEGER DEFAULT 0,
    cors            TEXT,
    csrf            INTEGER DEFAULT 1,
    controller_class TEXT,
    method_name     TEXT NOT NULL,
    module_name     TEXT,
    file_path       TEXT NOT NULL,
    line_number     INTEGER,
    path_params     TEXT DEFAULT '[]'   -- JSON
);
CREATE INDEX IF NOT EXISTS idx_routes_pattern ON http_routes(route_pattern);
CREATE INDEX IF NOT EXISTS idx_routes_module ON http_routes(module_name);

-- ── Actions table ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    action_type     TEXT NOT NULL,      -- act_window/server/client/report/url
    name            TEXT,
    -- act_window
    res_model       TEXT,
    view_mode       TEXT,
    domain          TEXT,
    context_expr    TEXT,
    target          TEXT,
    res_id          INTEGER,
    view_ids        TEXT,               -- JSON
    -- server action
    binding_model   TEXT,
    binding_views   TEXT,               -- JSON
    server_code     TEXT,
    server_method   TEXT,
    -- client action
    tag             TEXT,
    -- report
    report_name     TEXT,
    report_model    TEXT,
    -- parsed refs
    domain_fields   TEXT DEFAULT '[]',  -- JSON
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_model ON actions(res_model);
CREATE INDEX IF NOT EXISTS idx_actions_xml_id ON actions(xml_id);

-- ── Menus table ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS menus (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT NOT NULL,
    parent_xml_id   TEXT,
    sequence        INTEGER DEFAULT 10,
    action_xml_id   TEXT,
    action_type     TEXT,
    res_model       TEXT,
    groups          TEXT DEFAULT '[]',  -- JSON
    web_icon        TEXT,
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_menus_xml_id ON menus(xml_id);
CREATE INDEX IF NOT EXISTS idx_menus_model ON menus(res_model);

-- ── Cron jobs table ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cron_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT,
    model_name      TEXT NOT NULL,
    method_name     TEXT NOT NULL,
    method_args     TEXT,               -- JSON
    interval_number INTEGER DEFAULT 1,
    interval_type   TEXT DEFAULT 'months',
    numbercall      INTEGER DEFAULT -1,
    doall           INTEGER DEFAULT 0,
    priority        INTEGER DEFAULT 5,
    active          INTEGER DEFAULT 1,
    module_name     TEXT,
    file_path       TEXT
);

-- ── QWeb templates table ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS qweb_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT NOT NULL,
    name            TEXT,
    inherit_id      TEXT,
    priority        INTEGER DEFAULT 16,
    is_primary      INTEGER DEFAULT 1,
    template_type   TEXT,               -- qweb/report/email/website
    report_model    TEXT,
    t_calls         TEXT DEFAULT '[]',  -- JSON
    t_fields        TEXT DEFAULT '[]',  -- JSON
    t_if_fields     TEXT DEFAULT '[]',  -- JSON
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_qweb_xml_id ON qweb_templates(xml_id);

-- ── Email templates table ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS email_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT,
    model_name      TEXT NOT NULL,
    subject         TEXT,
    body_field_refs TEXT DEFAULT '[]',  -- JSON: parsed ${record.field} refs
    email_from      TEXT,
    email_to        TEXT,
    reply_to        TEXT,
    report_template TEXT,
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_model ON email_templates(model_name);

-- ── View element refs table ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS view_element_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    view_xml_id     TEXT NOT NULL,
    element_type    TEXT NOT NULL,      -- field/button/xpath/filter/etc.
    -- field
    field_name      TEXT,
    widget_name     TEXT,
    field_options   TEXT,
    field_attrs     TEXT,
    -- button
    button_name     TEXT,
    button_type     TEXT DEFAULT 'object',
    button_action   TEXT,
    button_confirm  TEXT,
    button_states   TEXT,
    button_groups   TEXT,
    -- xpath
    xpath_expr      TEXT,
    -- filter
    filter_domain   TEXT,
    filter_fields   TEXT DEFAULT '[]',
    -- visibility
    groups_attr     TEXT,
    invisible_expr  TEXT,
    attrs_expr      TEXT,
    -- position
    parent_element  TEXT,
    depth           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ver_view ON view_element_refs(view_xml_id);
CREATE INDEX IF NOT EXISTS idx_ver_button ON view_element_refs(button_name);
CREATE INDEX IF NOT EXISTS idx_ver_field ON view_element_refs(field_name);

-- ── Field groups map ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS field_groups_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    group_xml_id    TEXT NOT NULL,
    source          TEXT CHECK(source IN ('field_def', 'view_attr')),
    module_name     TEXT,
    UNIQUE(model_name, field_name, group_xml_id, source)
);

-- ── Selection extensions ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS selection_extensions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    added_values    TEXT NOT NULL,      -- JSON
    ondelete_map    TEXT,               -- JSON
    defined_in_module TEXT NOT NULL,
    file_path       TEXT,
    line_number     INTEGER
);

-- ── Context dependencies ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS context_dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    field_name      TEXT,
    method_name     TEXT,
    context_keys    TEXT NOT NULL,      -- JSON
    module_name     TEXT,
    file_path       TEXT
);

-- ── JS components ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS js_components (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    component_type  TEXT,
    widget_name     TEXT,
    handled_types   TEXT,               -- JSON
    component_class TEXT,
    action_tag      TEXT,
    target_model    TEXT,
    target_method   TEXT,
    target_route    TEXT,
    module_name     TEXT,
    file_path       TEXT,
    line_number     INTEGER
);

-- ── Access control tables ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS access_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT,
    model_name      TEXT NOT NULL,
    group_xml_id    TEXT,               -- NULL = all users
    perm_read       INTEGER DEFAULT 0,
    perm_write      INTEGER DEFAULT 0,
    perm_create     INTEGER DEFAULT 0,
    perm_unlink     INTEGER DEFAULT 0,
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_model ON access_rules(model_name);

CREATE TABLE IF NOT EXISTS record_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    xml_id          TEXT,
    name            TEXT,
    model_name      TEXT NOT NULL,
    domain_force    TEXT,
    groups          TEXT DEFAULT '[]',  -- JSON
    perm_read       INTEGER DEFAULT 1,
    perm_write      INTEGER DEFAULT 1,
    perm_create     INTEGER DEFAULT 1,
    perm_unlink     INTEGER DEFAULT 1,
    module_name     TEXT,
    file_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rrule_model ON record_rules(model_name);

-- ── Related field paths ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS related_field_paths (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_model    TEXT NOT NULL,
    source_field    TEXT NOT NULL,
    path            TEXT NOT NULL,
    step_1_model    TEXT,
    step_1_field    TEXT,
    step_2_model    TEXT,
    step_2_field    TEXT,
    terminal_model  TEXT,
    terminal_field  TEXT,
    terminal_type   TEXT,
    fully_resolved  INTEGER DEFAULT 0,
    broken_at       TEXT
);

-- ── State machines ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS state_machines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT NOT NULL,
    field_name      TEXT NOT NULL DEFAULT 'state',
    states          TEXT NOT NULL,      -- JSON: [['draft','Draft'],...]
    transitions     TEXT DEFAULT '[]',  -- JSON: [{from,to,method,button}]
    module_name     TEXT,
    UNIQUE(model_name, field_name)
);

-- ── FTS5 virtual table ───────────────────────────────────────────────────────

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    entity_type,    -- 'model','field','method','view','route'
    entity_name,
    model_context,
    content,        -- description, help, label etc.
    module_name,
    tokenize = 'porter unicode61'
);

-- ── File hashes (incremental indexing) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS file_hashes (
    module_name     TEXT PRIMARY KEY,
    hash            TEXT NOT NULL,      -- xxh3-64 of all source files in module
    indexed_at      TEXT NOT NULL       -- ISO-8601 UTC timestamp
);

-- ── Metadata ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS index_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""

DROP_ALL_SQL = """
DROP TABLE IF EXISTS search_index;
DROP TABLE IF EXISTS file_hashes;
DROP TABLE IF EXISTS index_meta;
DROP TABLE IF EXISTS related_field_paths;
DROP TABLE IF EXISTS state_machines;
DROP TABLE IF EXISTS record_rules;
DROP TABLE IF EXISTS access_rules;
DROP TABLE IF EXISTS js_components;
DROP TABLE IF EXISTS context_dependencies;
DROP TABLE IF EXISTS selection_extensions;
DROP TABLE IF EXISTS field_groups_map;
DROP TABLE IF EXISTS view_element_refs;
DROP TABLE IF EXISTS email_templates;
DROP TABLE IF EXISTS qweb_templates;
DROP TABLE IF EXISTS cron_jobs;
DROP TABLE IF EXISTS menus;
DROP TABLE IF EXISTS actions;
DROP TABLE IF EXISTS http_routes;
DROP TABLE IF EXISTS decorators_detail;
DROP TABLE IF EXISTS module_deps;
DROP TABLE IF EXISTS views;
DROP TABLE IF EXISTS methods;
DROP TABLE IF EXISTS fields;
DROP TABLE IF EXISTS models;
DROP TABLE IF EXISTS modules;
"""
