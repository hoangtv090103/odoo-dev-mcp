"""MCP Prompts — Odoo workflow templates for AI assistants.

Registers 5 guided workflow prompts onto a FastMCP instance.  Each prompt
returns a rich system message that primes the AI with a step-by-step plan
and the exact tool-call sequence to answer the user's goal.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP, project_name: str = "") -> None:
    """Register all 5 Odoo workflow prompts onto a FastMCP instance.

    Args:
        mcp:          The FastMCP server instance.
        project_name: Optional project name to personalise the prompts.
    """
    ctx = f" in project '{project_name}'" if project_name else ""

    # ── 1. analyze_odoo_model ─────────────────────────────────────────────────

    @mcp.prompt(
        description=(
            "Full deep-dive analysis of an Odoo model: fields, methods, "
            "state machine, security, views, dependencies, and related models."
        )
    )
    def analyze_odoo_model(model_name: str) -> str:
        """Guide the AI through a comprehensive model analysis."""
        return f"""You are an expert Odoo developer analysing the model **{model_name}**{ctx}.

Follow this step-by-step tool-call sequence to build a complete picture:

**Step 1 — Entry point**
Call `get_project_context(focus_model="{model_name}")` first.
This gives index health, quick facts (field/method count, has_sm, has_acl),
and the suggested tool chain for this model.

**Step 2 — Field and method overview**
Call `get_model_schema("{model_name}", compact=True)` for a one-line-per-field
summary.  If you need full detail on a specific field, call it again without
compact=True and use fields_limit to narrow the result.

**Step 3 — Behaviour layer**
• If the model has a state machine (has_sm=True from Step 1):
  call `get_state_machine("{model_name}")` — states, transitions, trigger methods.
• For key computed fields: call `trace_compute_chain("{model_name}", "<field>")`.
• For important methods: call `get_method_logic("{model_name}", "<method>")`.

**Step 4 — Security**
If the model has ACL rules (has_acl=True from Step 1):
call `get_access_control("{model_name}")` — model ACLs, record rules, field groups.

**Step 5 — Views**
Call `resolve_xml_view("{model_name}", "form")` for the merged form view.
This reveals field visibility, buttons, and inherited customisations.

**Step 6 — Validation**
Call `get_constraints("{model_name}")` — @api.constrains, SQL constraints, onchange validators.

**Step 7 — Graph walk (optional, for dependency understanding)**
Call `trace_odoo_path("{model_name}", depth=2, edge_types=["field_rel","inherit"])`
to see the relational neighbourhood.

**Synthesis**
After gathering data, summarise:
- Purpose and business role of the model
- Key fields and their types/compute logic
- State lifecycle (if applicable)
- Who can access/modify records and when
- Main UI entry points (views, actions, menus)
- Change risk: which other models depend on this one
"""

    # ── 2. debug_field_issue ──────────────────────────────────────────────────

    @mcp.prompt(
        description=(
            "Diagnose why an Odoo field has the wrong value, isn't updating, "
            "or behaves unexpectedly. Traces compute chain, onchange, and visibility."
        )
    )
    def debug_field_issue(model_name: str, field_name: str) -> str:
        """Guide the AI through systematic field debugging."""
        return f"""You are debugging why the field **{field_name}** on **{model_name}**{ctx}
has an unexpected value or is not updating correctly.

Work through these diagnostic steps in order:

**Step 1 — Field definition**
Call `get_model_schema("{model_name}", compact=False, fields_limit=1)`
— filter mentally for `{field_name}` to check:
  - field type and comodel (if relational)
  - whether it is `compute`, `related`, or plain stored
  - `required` / `readonly` flags
  - `groups` restriction (could be causing invisible/readonly behaviour)

**Step 2 — Compute chain (if compute field)**
Call `trace_compute_chain("{model_name}", "{field_name}")`.
This shows:
  - The compute method name
  - `@api.depends` fields that trigger recomputation
  - `@api.depends_context` context keys
  - Whether the field is stored or always re-evaluated

**Step 3 — Onchange triggers**
Call `get_method_logic("{model_name}", "_onchange_{field_name}")` if it exists.
Also search: `search_odoo_entities("onchange {field_name}", types=["method"], module=None)`.

**Step 4 — Visibility and editability**
Call `get_field_visibility("{model_name}", "{field_name}")`.
This reveals all views where the field appears and any `attrs` rules
(invisible, required, readonly) that could hide or lock the value.

**Step 5 — Constraints**
Call `get_constraints("{model_name}")` — check for @api.constrains that
validate or limit what values are accepted.

**Step 6 — Related field path (if `related`)**
If the field is a `related` field, trace the dotted path manually through
`get_model_schema` calls on each intermediate model to find where the
source value lives and whether it is stored correctly.

**Diagnosis output**
Summarise:
- Root cause of the unexpected behaviour
- The exact trigger/dependency chain
- Any view attrs hiding or forcing the value
- Recommended fix with file + line reference
"""

    # ── 3. plan_model_change ──────────────────────────────────────────────────

    @mcp.prompt(
        description=(
            "Plan a safe change to an Odoo model: assess blast radius, "
            "identify all dependents, check security implications."
        )
    )
    def plan_model_change(
        model_name: str,
        change_description: str,
    ) -> str:
        """Guide the AI through pre-change impact assessment."""
        return f"""You are planning the following change to **{model_name}**{ctx}:

> {change_description}

Before writing any code, perform this impact assessment:

**Step 1 — Current state snapshot**
Call `get_project_context(focus_model="{model_name}")`.
Note the field count, method count, and whether it has a state machine or ACL.

**Step 2 — Blast radius**
Call `analyze_change_impact("{model_name}")`.
This reveals:
  - Views that reference the model
  - Methods that call into this model
  - Other models that have relational fields pointing here
  - Downstream compute chains that would be invalidated

If the change targets a specific field, call:
  `analyze_change_impact("{model_name}", field_name="<field>")`.

**Step 3 — Dependency graph**
Call `trace_odoo_path("{model_name}", depth=2, edge_types=["field_rel","inherit","compute"])`.
This maps out every model that directly or indirectly depends on {model_name}.

**Step 4 — State machine (if applicable)**
Call `get_state_machine("{model_name}")`.
Identify any state transitions that the change might affect or break.

**Step 5 — Security review**
Call `get_access_control("{model_name}")`.
Check whether the change would affect:
  - Which groups can read/write the changed field
  - Whether record rules filter on the changed field/state

**Step 6 — Constraints**
Call `get_constraints("{model_name}")`.
Identify existing validations that might conflict with the change.

**Change plan output**
Produce a structured plan:
1. Summary of what will change and why
2. Files to modify (with module and approximate line numbers)
3. Tests to update or add
4. Migration notes (if schema changes are involved)
5. Risk rating: LOW / MEDIUM / HIGH with justification
"""

    # ── 4. understand_business_flow ──────────────────────────────────────────

    @mcp.prompt(
        description=(
            "Understand an Odoo business process end-to-end: trace the full "
            "lifecycle from creation through state transitions to completion."
        )
    )
    def understand_business_flow(
        model_name: str,
        flow_description: str = "",
    ) -> str:
        """Guide the AI through tracing an end-to-end business process."""
        flow_hint = f"\nBusiness context: {flow_description}\n" if flow_description else ""
        return f"""You are mapping the end-to-end business flow for **{model_name}**{ctx}.{flow_hint}

Follow this sequence to trace the complete lifecycle:

**Step 1 — Overview**
Call `get_project_context(focus_model="{model_name}")`.
The focus block tells you whether there is a state machine, cron jobs,
and the list of suggested tools for this model.

**Step 2 — State lifecycle**
Call `get_state_machine("{model_name}")`.
This gives you ALL states, transitions, and the buttons/methods that trigger each.
Draw the lifecycle mentally:  draft → confirmed → done (or similar).

**Step 3 — Action buttons**
For each key button found in the state machine, call:
  `trace_button_to_method("<view_xml_id>", "<button_name>")`.
This traces button → action/method → what Python code runs.

**Step 4 — Method logic**
For each important method (action_confirm, action_done, etc.) call:
  `get_method_logic("{model_name}", "<method_name>")`.
Note: state transitions, ORM calls, validation raises, cron eligibility.

**Step 5 — Related models**
Call `trace_odoo_path("{model_name}", depth=2, edge_types=["field_rel","state","action"])`.
This shows which models are created/updated at each stage of the flow.

**Step 6 — Actions, menus, cron**
Call `get_model_actions("{model_name}")` to find:
  - Window actions (UI entry points)
  - Server actions (automation)
  - Cron jobs (scheduled processing)
  - Reports (PDF/XLSX generation)

**Step 7 — View sequence**
Call `resolve_xml_view("{model_name}", "form")` and
`resolve_xml_view("{model_name}", "tree")` to see the full UI surface.

**Business flow output**
Produce a narrative that covers:
- How a record is created (who, from where, with what data)
- Each state transition: trigger → method → side effects → next state
- Which models are created/modified at each step
- Scheduled automation (cron jobs)
- How the flow ends (confirmed, cancelled, archived, etc.)
"""

    # ── 5. security_review ────────────────────────────────────────────────────

    @mcp.prompt(
        description=(
            "Comprehensive security audit of an Odoo model: ACLs, record rules, "
            "field-level groups, exposed HTTP routes, and validation constraints."
        )
    )
    def security_review(
        model_name: str,
        security_concern: str = "",
    ) -> str:
        """Guide the AI through a thorough security audit."""
        concern_hint = f"\nSpecific concern: {security_concern}\n" if security_concern else ""
        return f"""You are performing a security audit of **{model_name}**{ctx}.{concern_hint}

Work through each security layer systematically:

**Layer 1 — Model-level access control**
Call `get_access_control("{model_name}")`.
Review:
  - Which security groups have create / read / write / unlink
  - Whether anonymous or public access is permitted
  - Record rules: domain filters that restrict per-user or per-company access
  - Field-level groups: fields visible only to certain groups

**Layer 2 — Field visibility in views**
For sensitive fields (e.g. price, salary, personal data), call:
  `get_field_visibility("{model_name}", "<field_name>")`.
Check for `groups=` attributes and state-based attrs that could expose data.

**Layer 3 — HTTP / JSON-RPC exposure**
Search for routes related to this model:
  `get_http_routes(module_name="<module>")` or
  `search_odoo_entities("{model_name}", types=["route"])`.
Review each route's `auth` type:
  - `public` — accessible without login (highest risk)
  - `user` — requires authenticated Odoo session
  - `none` — no auth check at all

**Layer 4 — Validation and constraints**
Call `get_constraints("{model_name}")`.
Check whether constraints adequately prevent:
  - Negative amounts / invalid states
  - Cross-model consistency violations
  - SQL-level CHECK constraints

**Layer 5 — Sensitive methods**
Search for methods that write sensitive data:
  `search_odoo_entities("write unlink sudo", types=["method"])` within this model's module.
Then call `get_method_logic("{model_name}", "<method>")` for suspicious ones.

**Layer 6 — State machine security**
Call `get_state_machine("{model_name}")`.
Verify that dangerous transitions (e.g. cancel, validate, approve) require
adequate group checks and cannot be triggered via unexpected buttons.

**Security report output**
Produce a risk-ranked finding list:
- CRITICAL: public/none auth routes, missing ACLs, sudo() without group check
- HIGH: overly broad record rules, sensitive fields without groups
- MEDIUM: missing SQL constraints, unchecked state transitions
- LOW: informational / best-practice gaps
For each finding: location (model/method/view), description, recommended fix.
"""
