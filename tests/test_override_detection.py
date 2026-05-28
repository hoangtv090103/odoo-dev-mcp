"""
Tests for override-detection features in get_method_logic and get_model_schema.

Fixture models used:
  benchmark.order          — defines action_confirm (not an override)
  benchmark.order.extended — inherits benchmark.order, overrides action_confirm WITH super()
  benchmark.mixin          — abstract, defines _compute_display_priority
"""
import textwrap
from pathlib import Path

import pytest

from odoo_dev_mcp.config import AddonsPathEntry, ProjectConfig
from odoo_dev_mcp.indexer.pipeline import run_full_index
from odoo_dev_mcp.tools.get_method_logic import get_method_logic
from odoo_dev_mcp.tools.get_model_schema import get_model_schema


# ── get_method_logic: override cases ─────────────────────────────────────────

async def test_override_detected(get_db):
    """benchmark.order.extended.action_confirm overrides benchmark.order."""
    result = await get_method_logic("benchmark.order.extended", "action_confirm", get_db)

    assert result.get("is_override") is True
    assert isinstance(result.get("overrides_from"), list)
    assert len(result["overrides_from"]) > 0
    assert any(entry["model"] == "benchmark.order" for entry in result["overrides_from"])


async def test_calls_super_on_override(get_db):
    """The override in benchmark.order.extended calls super(), so no bug is flagged."""
    result = await get_method_logic("benchmark.order.extended", "action_confirm", get_db)

    assert result.get("calls_super") is True
    assert result.get("missing_super_call") is False


async def test_non_override_base_method(get_db):
    """action_confirm on benchmark.order is NOT an override — it is the original."""
    result = await get_method_logic("benchmark.order", "action_confirm", get_db)

    assert result.get("is_override") is False
    assert result.get("overrides_from") == []
    assert result.get("missing_super_call") is False


async def test_module_field_present(get_db):
    """Both base and overriding methods should report which module they belong to."""
    base = await get_method_logic("benchmark.order", "action_confirm", get_db)
    override = await get_method_logic("benchmark.order.extended", "action_confirm", get_db)

    assert base.get("module") is not None
    assert override.get("module") is not None


async def test_missing_super_call_flagged(tmp_path):
    """A child that overrides action_confirm WITHOUT super() gets missing_super_call=True."""
    # Build a minimal two-model addon in a temp directory
    addon = tmp_path / "no_super_addon"
    models_dir = addon / "models"
    models_dir.mkdir(parents=True)

    (addon / "__manifest__.py").write_text(
        "{'name': 'No Super Test', 'version': '17.0.1.0.0', 'depends': ['base']}"
    )
    (addon / "__init__.py").write_text("from . import models\n")
    (models_dir / "__init__.py").write_text(
        "from . import parent_model\nfrom . import child_model\n"
    )
    (models_dir / "parent_model.py").write_text(textwrap.dedent("""\
        from odoo import models

        class NsParent(models.Model):
            _name = 'ns.parent'
            _description = 'No-super parent'

            def action_confirm(self):
                self.write({'state': 'confirmed'})
    """))
    (models_dir / "child_model.py").write_text(textwrap.dedent("""\
        from odoo import models

        class NsChild(models.Model):
            _name = 'ns.child'
            _description = 'No-super child'
            _inherit = 'ns.parent'

            def action_confirm(self):
                # Intentionally NOT calling super() — this is the pattern under test
                self.write({'state': 'draft'})
    """))

    config = ProjectConfig(
        name="pytest-no-super",
        addons_paths=[AddonsPathEntry(path=tmp_path)],
        root_path=tmp_path,
    )
    run_full_index(config, reset=True)
    get_db_tmp = lambda: config.db_path

    result = await get_method_logic("ns.child", "action_confirm", get_db_tmp)

    assert result.get("is_override") is True, "should detect override"
    assert result.get("calls_super") is False, "no super() call present"
    assert result.get("missing_super_call") is True, "bug pattern should be flagged"


# ── get_model_schema: methods list ───────────────────────────────────────────

async def test_own_methods_in_schema(get_db):
    """get_model_schema returns a 'methods' list; own methods have no inherited_from."""
    result = await get_model_schema("benchmark.order", get_db)

    assert "methods" in result
    methods = result["methods"]
    assert len(methods) > 0

    own = [m for m in methods if "inherited_from" not in m]
    names = [m["name"] for m in own]
    assert "action_confirm" in names, "action_confirm is an own method on benchmark.order"


async def test_inherited_methods_have_attribution(get_db):
    """Methods pulled from parent models carry an inherited_from key."""
    result = await get_model_schema("benchmark.order.extended", get_db)

    inherited = [m for m in result.get("methods", []) if "inherited_from" in m]
    assert len(inherited) > 0

    inherited_names = {m["name"] for m in inherited}
    assert "_compute_amounts" in inherited_names
    assert "action_cancel" in inherited_names

    compute_amounts = next(m for m in inherited if m["name"] == "_compute_amounts")
    assert compute_amounts["inherited_from"] == "benchmark.order"

    action_cancel = next(m for m in inherited if m["name"] == "action_cancel")
    assert action_cancel["inherited_from"] == "benchmark.order"


async def test_transitive_mixin_methods(get_db):
    """Methods from benchmark.mixin appear via transitive inheritance on benchmark.order.extended."""
    result = await get_model_schema("benchmark.order.extended", get_db)

    inherited = {m["name"]: m for m in result.get("methods", []) if "inherited_from" in m}

    assert "_compute_display_priority" in inherited, (
        "_compute_display_priority should be transitively inherited from benchmark.mixin"
    )
    assert inherited["_compute_display_priority"]["inherited_from"] == "benchmark.mixin"


async def test_override_annotation_on_own_method(get_db):
    """action_confirm on benchmark.order.extended should carry an 'overrides' annotation."""
    result = await get_model_schema("benchmark.order.extended", get_db)

    own = [m for m in result.get("methods", []) if "inherited_from" not in m]
    action_confirm = next((m for m in own if m["name"] == "action_confirm"), None)

    assert action_confirm is not None, "action_confirm should be an own method"
    assert "overrides" in action_confirm, "should be annotated as an override"
    overrides_models = [entry["model"] for entry in action_confirm["overrides"]]
    assert "benchmark.order" in overrides_models


async def test_include_inherited_false_suppresses_inherited_methods(get_db):
    """include_inherited=False returns only own methods; no inherited_from entries."""
    result = await get_model_schema(
        "benchmark.order.extended", get_db, include_inherited=False
    )

    methods = result.get("methods", [])
    assert all("inherited_from" not in m for m in methods), (
        "include_inherited=False must not include inherited methods"
    )
    # Only action_confirm is defined directly on benchmark.order.extended
    assert len(methods) == 1
    assert methods[0]["name"] == "action_confirm"
