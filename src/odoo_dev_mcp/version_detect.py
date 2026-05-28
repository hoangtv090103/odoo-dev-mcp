"""Odoo version detection heuristics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# Matches  version = '16.0'  or  version='16.0.1.0.0'  etc.
_RELEASE_PY_RE = re.compile(
    r"""version\s*=\s*['"](\d{1,2}\.\d)""",
    re.MULTILINE,
)

# Matches  version='16.0'  in setup.py  (no spaces around =)
_SETUP_PY_RE = re.compile(
    r"""version\s*=\s*['"](\d{1,2}\.\d)""",
    re.MULTILINE,
)

# Matches  'version': '16.0.x.y.z'  inside a manifest dict
_MANIFEST_RE = re.compile(
    r"""['"]\s*version\s*['"]\s*:\s*['"]\s*(\d{1,2}\.\d)\.""",
    re.MULTILINE,
)

# Odoo version string normalised to X.Y (e.g. "16.0", "17.0")
_VERSION_PATTERN = re.compile(r"^\d{1,2}\.\d$")


def _read_text_safe(path: Path) -> Optional[str]:
    """Return file contents as a string, or *None* on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parents(addons_paths: list[Path]) -> list[Path]:
    """Collect unique immediate parent directories of each addons path."""
    seen: set[Path] = set()
    result: list[Path] = []
    for ap in addons_paths:
        parent = ap.resolve().parent
        if parent not in seen:
            seen.add(parent)
            result.append(parent)
    return result


def detect_odoo_version_hint(addons_paths: list[Path]) -> Optional[str]:
    """Return an Odoo version string (e.g. ``"16.0"``) inferred from the
    project tree, or *None* when the version cannot be determined.

    Detection order (first match wins):

    1. ``<parent>/odoo/release.py`` — authoritative ``version = 'X.Y'``
    2. ``<parent>/setup.py`` — ``version='X.Y'``
    3. Manifest files (``__manifest__.py``, ``__openerp__.py``) — version prefix
    4. OWL indicator: ``web/static/src/views/fields/`` present → ``"17.0+"``
    """
    parents = _parents(addons_paths)

    # ------------------------------------------------------------------
    # 1. odoo/release.py
    # ------------------------------------------------------------------
    for parent in parents:
        release_py = parent / "odoo" / "release.py"
        text = _read_text_safe(release_py)
        if text:
            m = _RELEASE_PY_RE.search(text)
            if m:
                version = m.group(1)
                if _VERSION_PATTERN.match(version):
                    return version

    # ------------------------------------------------------------------
    # 2. setup.py
    # ------------------------------------------------------------------
    for parent in parents:
        setup_py = parent / "setup.py"
        text = _read_text_safe(setup_py)
        if text:
            m = _SETUP_PY_RE.search(text)
            if m:
                version = m.group(1)
                if _VERSION_PATTERN.match(version):
                    return version

    # ------------------------------------------------------------------
    # 3. Manifest files inside addons directories
    # ------------------------------------------------------------------
    version_counts: dict[str, int] = {}
    for ap in addons_paths:
        ap = ap.resolve()
        if not ap.is_dir():
            continue
        for manifest_name in ("__manifest__.py", "__openerp__.py"):
            for manifest_path in ap.rglob(manifest_name):
                text = _read_text_safe(manifest_path)
                if not text:
                    continue
                m = _MANIFEST_RE.search(text)
                if m:
                    v = m.group(1)
                    if _VERSION_PATTERN.match(v):
                        version_counts[v] = version_counts.get(v, 0) + 1

    if version_counts:
        # Return the version that appears most often across manifests
        return max(version_counts, key=lambda v: version_counts[v])

    # ------------------------------------------------------------------
    # 4. OWL indicator (Odoo 17+)
    # ------------------------------------------------------------------
    for ap in addons_paths:
        ap = ap.resolve()
        owl_dir = ap / "web" / "static" / "src" / "views" / "fields"
        if owl_dir.is_dir():
            return "17.0+"

    # Also check inside each parent's addons subdir
    for parent in parents:
        owl_dir = parent / "addons" / "web" / "static" / "src" / "views" / "fields"
        if owl_dir.is_dir():
            return "17.0+"

    return None
