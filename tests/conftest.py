"""
Shared pytest fixtures for OdooDevMCP tests.
"""
import sys
from pathlib import Path

import pytest

# Ensure the src package is importable without an editable install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from odoo_dev_mcp.config import AddonsPathEntry, ProjectConfig
from odoo_dev_mcp.indexer.pipeline import run_full_index

FIXTURE_ADDON_ROOT = Path(__file__).parent.parent / "evaluate" / "fixtures"


@pytest.fixture(scope="module")
def benchmark_config() -> ProjectConfig:
    """Build the benchmark fixture index once per test module and reuse it."""
    config = ProjectConfig(
        name="pytest-benchmark",
        addons_paths=[AddonsPathEntry(path=FIXTURE_ADDON_ROOT)],
        root_path=FIXTURE_ADDON_ROOT,
    )
    run_full_index(config, reset=True)
    return config


@pytest.fixture(scope="module")
def get_db(benchmark_config):
    """Lazy DB path callable — all tool calls receive this."""
    return lambda: benchmark_config.db_path
