"""
Scan addons directories for Odoo modules.

A module is any directory containing __manifest__.py or __openerp__.py.
Manifests are parsed with ast.literal_eval (safe, no exec).
"""

from __future__ import annotations

import ast
import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import ProjectConfig

logger = logging.getLogger(__name__)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class ModuleRecord:
    name: str
    path: Path
    version: Optional[str]
    category: Optional[str]
    depends: list[str]
    auto_install: bool
    installable: bool
    application: bool
    summary: Optional[str]
    description: Optional[str]
    author: Optional[str]
    website: Optional[str]
    python_files: list[Path]     # all .py files in module
    xml_files: list[Path]        # all .xml files in data/, views/, security/, etc.
    csv_files: list[Path]        # ir.model.access.csv files
    js_files: list[Path]         # static/src/**/*.js (not lib/, tests/)
    addons_path: Path            # which addons_path this module came from


# ── Manifest parsing ──────────────────────────────────────────────────────────

def _parse_manifest(manifest_path: Path) -> dict:
    """Parse a manifest file safely using ast.literal_eval."""
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
        data = ast.literal_eval(text)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.debug("Failed to parse manifest %s: %s", manifest_path, exc)
    return {}


# ── File collection ───────────────────────────────────────────────────────────

def _collect_python_files(module_path: Path) -> list[Path]:
    """Collect all .py files within a module directory."""
    result: list[Path] = []
    for p in sorted(module_path.rglob("*.py")):
        # Skip compiled bytecode directories
        if "__pycache__" in p.parts:
            continue
        result.append(p)
    return result


def _collect_xml_files(module_path: Path) -> list[Path]:
    """Collect XML files from the standard Odoo data directories."""
    result: list[Path] = []
    # Include all XML files recursively — Odoo modules can put data anywhere
    for p in sorted(module_path.rglob("*.xml")):
        parts_lower = [part.lower() for part in p.parts]
        # Skip test directories
        if "tests" in parts_lower or "test" in parts_lower:
            continue
        result.append(p)
    return result


def _collect_csv_files(module_path: Path) -> list[Path]:
    """Collect CSV access files (ir.model.access.csv)."""
    result: list[Path] = []
    for p in sorted(module_path.rglob("*.csv")):
        # Only collect ir.model.access files
        if p.name == "ir.model.access.csv":
            result.append(p)
    return result


def _collect_js_files(module_path: Path) -> list[Path]:
    """Collect JS files from static/src/, excluding lib/ and tests/ subdirs."""
    result: list[Path] = []
    static_src = module_path / "static" / "src"
    if not static_src.is_dir():
        return result
    for p in sorted(static_src.rglob("*.js")):
        parts = p.relative_to(static_src).parts
        # Skip lib/ and tests/ directories
        if parts and parts[0].lower() in ("lib", "tests", "test"):
            continue
        result.append(p)
    return result


# ── Exclusion check ───────────────────────────────────────────────────────────

def _is_excluded(module_path: Path, patterns: list[str]) -> bool:
    """Return True if the module *name* matches any exclude pattern.

    Patterns like ``*/test_*`` are intended to match the module directory
    name, not the full absolute path — matching the full path would falsely
    exclude modules whose parent directory happens to contain a pattern word.
    We therefore extract the trailing name-portion of each pattern and test
    only against ``module_path.name``.
    """
    module_name = module_path.name
    for pattern in patterns:
        # Strip any leading "*/" glob prefix so '*/test_*' → 'test_*'
        name_pattern = pattern.lstrip("*/")
        if name_pattern and fnmatch.fnmatch(module_name, name_pattern):
            return True
    return False


# ── Module builder ────────────────────────────────────────────────────────────

def _build_module_record(
    module_dir: Path,
    addons_path: Path,
    exclude_patterns: list[str],
) -> Optional[ModuleRecord]:
    """Build a ModuleRecord from a module directory, or None if excluded/invalid."""
    if _is_excluded(module_dir, exclude_patterns):
        return None

    # Find manifest
    manifest_path: Optional[Path] = None
    for name in ("__manifest__.py", "__openerp__.py"):
        candidate = module_dir / name
        if candidate.is_file():
            manifest_path = candidate
            break

    if manifest_path is None:
        return None

    manifest = _parse_manifest(manifest_path)

    # Extract manifest fields
    version: Optional[str] = manifest.get("version")
    if version:
        version = str(version)
    category: Optional[str] = manifest.get("category")
    if category:
        category = str(category)

    depends_raw = manifest.get("depends", [])
    if isinstance(depends_raw, (list, tuple)):
        depends = [str(d) for d in depends_raw]
    else:
        depends = []

    auto_install_raw = manifest.get("auto_install", False)
    # auto_install can be a bool or a list of modules
    auto_install = bool(auto_install_raw) if not isinstance(auto_install_raw, list) else bool(auto_install_raw)

    installable = bool(manifest.get("installable", True))
    application = bool(manifest.get("application", False))

    summary_raw = manifest.get("summary", manifest.get("name", ""))
    summary: Optional[str] = str(summary_raw).strip() if summary_raw else None

    description_raw = manifest.get("description", "")
    description: Optional[str] = str(description_raw).strip() if description_raw else None

    author_raw = manifest.get("author", "")
    author: Optional[str] = str(author_raw).strip() if author_raw else None

    website_raw = manifest.get("website", "")
    website: Optional[str] = str(website_raw).strip() if website_raw else None

    return ModuleRecord(
        name=module_dir.name,
        path=module_dir,
        version=version,
        category=category,
        depends=depends,
        auto_install=auto_install,
        installable=installable,
        application=application,
        summary=summary,
        description=description,
        author=author,
        website=website,
        python_files=_collect_python_files(module_dir),
        xml_files=_collect_xml_files(module_dir),
        csv_files=_collect_csv_files(module_dir),
        js_files=_collect_js_files(module_dir),
        addons_path=addons_path,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def scan_addons_path(
    addons_path: Path,
    exclude_patterns: Optional[list[str]] = None,
) -> list[ModuleRecord]:
    """Scan one addons directory for Odoo modules.

    A module is any direct subdirectory containing __manifest__.py or
    __openerp__.py.
    """
    if exclude_patterns is None:
        exclude_patterns = []

    addons_path = addons_path.resolve()
    if not addons_path.is_dir():
        logger.warning("Addons path does not exist or is not a directory: %s", addons_path)
        return []

    modules: list[ModuleRecord] = []

    # Only look at immediate children — Odoo addons paths are not recursive
    for entry in sorted(addons_path.iterdir()):
        if not entry.is_dir():
            continue
        record = _build_module_record(entry, addons_path, exclude_patterns)
        if record is not None:
            modules.append(record)

    logger.debug("Scanned %s: found %d modules", addons_path, len(modules))
    return modules


def scan_all_paths(config: ProjectConfig) -> list[ModuleRecord]:
    """Scan all addons paths in a ProjectConfig.

    Modules with duplicate names (from different paths) are both included;
    the last one wins only in the context of the indexer's upsert logic.
    """
    all_modules: list[ModuleRecord] = []
    exclude = config.exclude_patterns or []

    for entry in config.addons_paths:
        path = entry.path.expanduser().resolve()
        modules = scan_addons_path(path, exclude)
        all_modules.extend(modules)

    logger.info(
        "scan_all_paths: %d addons paths, %d modules total",
        len(config.addons_paths),
        len(all_modules),
    )
    return all_modules
