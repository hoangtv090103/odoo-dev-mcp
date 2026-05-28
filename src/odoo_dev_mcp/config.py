"""Configuration management."""

from __future__ import annotations

import re
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import platformdirs

try:
    import xxhash

    def _hash_str(s: str) -> str:
        return xxhash.xxh3_64(s.encode()).hexdigest()

except ImportError:  # pragma: no cover
    import hashlib

    def _hash_str(s: str) -> str:  # type: ignore[misc]
        return hashlib.md5(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_data_dir() -> Path:
    """Return ``~/.local/share/odoo-dev-mcp/`` (XDG-aware via platformdirs)."""
    return Path(platformdirs.user_data_dir("odoo-dev-mcp"))


def get_registry_path() -> Path:
    """Return the path to the global project registry database."""
    return get_data_dir() / "registry.db"


def project_index_dir(root_path: Path) -> Path:
    """Return the hidden ``.odoo-dev-mcp/`` directory inside a project root.

    This directory holds the SQLite knowledge-graph index and any other
    tool-generated artefacts.  It is created on demand.
    """
    idx = root_path.resolve() / ".odoo-dev-mcp"
    idx.mkdir(parents=True, exist_ok=True)
    return idx


def get_global_config_path() -> Path:
    """Return ``~/.config/odoo-dev-mcp/config.toml`` (XDG-aware)."""
    return Path(platformdirs.user_config_dir("odoo-dev-mcp")) / "config.toml"


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert arbitrary text to a filesystem-safe slug (lowercase, hyphens)."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AddonsPathEntry:
    """A single addons directory, optionally labelled."""

    path: Path
    label: str = ""


@dataclass
class ProjectConfig:
    """Full configuration for one Odoo project."""

    name: str
    addons_paths: list[AddonsPathEntry]
    config_file_path: Optional[Path] = None
    root_path: Path = field(default_factory=Path.cwd)
    exclude_patterns: list[str] = field(
        default_factory=lambda: ["*/test_*", "*/demo_data"]
    )
    watch_enabled: bool = True
    watch_debounce_ms: int = 250
    js_parsing: bool = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def all_paths(self) -> list[Path]:
        """All addons paths resolved to absolute ``Path`` objects."""
        return [entry.path.expanduser().resolve() for entry in self.addons_paths]

    @property
    def addons_hash(self) -> str:
        """Stable xxh3-64 (or MD5) hex digest of the sorted resolved paths."""
        joined = ":".join(str(p) for p in sorted(self.all_paths))
        return _hash_str(joined)

    @property
    def db_path(self) -> Path:
        """Absolute path to this project's SQLite index file.

        Stored at ``<root_path>/.odoo-dev-mcp/index.db`` so the index lives
        alongside the project source.  Add ``.odoo-dev-mcp/`` to ``.gitignore``
        to keep it out of version control.
        """
        return project_index_dir(self.root_path) / "index.db"


@dataclass
class GlobalConfig:
    """Global defaults loaded from ``~/.config/odoo-dev-mcp/config.toml``."""

    watch_enabled: bool = True
    debounce_ms: int = 250
    fts_detail: str = "column"
    js_parsing: bool = False
    index_workers: int = 0
    max_index_size_mb: int = 500
    index_dir: Optional[Path] = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_global_config() -> GlobalConfig:
    """Read global config, returning defaults when the file is absent."""
    path = get_global_config_path()
    if not path.exists():
        return GlobalConfig()

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    cfg = GlobalConfig()
    watch = data.get("watch", {})
    cfg.watch_enabled = bool(watch.get("enabled", cfg.watch_enabled))
    cfg.debounce_ms = int(watch.get("debounce_ms", cfg.debounce_ms))

    index = data.get("index", {})
    cfg.fts_detail = str(index.get("fts_detail", cfg.fts_detail))
    cfg.js_parsing = bool(index.get("js_parsing", cfg.js_parsing))
    cfg.index_workers = int(index.get("workers", cfg.index_workers))
    cfg.max_index_size_mb = int(index.get("max_size_mb", cfg.max_index_size_mb))
    if "dir" in index:
        cfg.index_dir = Path(index["dir"]).expanduser().resolve()

    return cfg


def find_project_config(start_dir: Path) -> Optional[Path]:
    """Traverse *up* from ``start_dir`` looking for ``.odoo-dev-mcp.toml``.

    Stops at the filesystem root or the user's home directory, whichever
    comes first.
    """
    home = Path.home()
    current = start_dir.resolve()
    while True:
        candidate = current / ".odoo-dev-mcp.toml"
        if candidate.is_file():
            return candidate
        # Stop at root or home
        if current == current.parent or current == home:
            break
        current = current.parent
    return None


def load_project_config(config_path: Path) -> ProjectConfig:
    """Parse a ``.odoo-dev-mcp.toml`` file and return a :class:`ProjectConfig`.

    Relative paths inside the TOML are resolved relative to the directory
    that contains the config file.
    """
    config_path = config_path.resolve()
    base_dir = config_path.parent

    with open(config_path, "rb") as fh:
        data = tomllib.load(fh)

    # [project]
    project_section = data.get("project", {})
    name: str = project_section.get("name", base_dir.name)
    root_path = Path(project_section.get("root_path", str(base_dir))).expanduser()
    if not root_path.is_absolute():
        root_path = (base_dir / root_path).resolve()
    else:
        root_path = root_path.resolve()

    # [[addons_path]]
    raw_paths: list[dict] = data.get("addons_path", [])
    entries: list[AddonsPathEntry] = []
    for item in raw_paths:
        raw = item.get("path", "")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        else:
            p = p.resolve()
        entries.append(AddonsPathEntry(path=p, label=item.get("label", "")))

    # [index]
    index_section = data.get("index", {})
    exclude_patterns: list[str] = index_section.get(
        "exclude_patterns", ["*/test_*", "*/demo_data"]
    )
    js_parsing: bool = bool(index_section.get("js_parsing", False))

    # [watch]
    watch_section = data.get("watch", {})
    watch_enabled: bool = bool(watch_section.get("enabled", True))
    watch_debounce_ms: int = int(watch_section.get("debounce_ms", 250))

    return ProjectConfig(
        name=name,
        addons_paths=entries,
        config_file_path=config_path,
        root_path=root_path,
        exclude_patterns=exclude_patterns,
        watch_enabled=watch_enabled,
        watch_debounce_ms=watch_debounce_ms,
        js_parsing=js_parsing,
    )


def resolve_project_config(
    addons_paths_str: Optional[str] = None,
    start_dir: Optional[Path] = None,
) -> ProjectConfig:
    """Resolve project configuration.

    Priority:
    1. ``addons_paths_str`` — colon-separated list of paths supplied via CLI.
    2. ``.odoo-dev-mcp.toml`` found by traversing up from ``start_dir``
       (defaults to ``Path.cwd()``).
    3. Raises :class:`ValueError` with a helpful message.
    """
    # 1. Explicit CLI paths take top priority
    if addons_paths_str:
        # Accept both ':' (colon) and ',' (comma) as separators
        sep = "," if "," in addons_paths_str and ":" not in addons_paths_str else ":"
        parts = [p.strip() for p in addons_paths_str.split(sep) if p.strip()]
        if not parts:
            raise ValueError(
                "No valid paths found. Use ':' or ',' to separate multiple paths."
            )
        entries = [
            AddonsPathEntry(path=Path(p).expanduser().resolve()) for p in parts
        ]
        cwd = (start_dir or Path.cwd()).resolve()
        return ProjectConfig(
            name=cwd.name,
            addons_paths=entries,
            root_path=cwd,
        )

    # 2. Look for .odoo-dev-mcp.toml
    cwd = (start_dir or Path.cwd()).resolve()
    config_path = find_project_config(cwd)
    if config_path is not None:
        return load_project_config(config_path)

    # 3. No config found
    raise ValueError(
        "No project configuration found.\n\n"
        "Options:\n"
        "  • Create a .odoo-dev-mcp.toml in your project root  (run: odoo-dev-mcp init)\n"
        "  • Pass --addons-paths /path/to/addons:/path/to/more\n"
        f"  • Searched upward from: {cwd}"
    )


def create_default_toml(
    root_path: Path,
    name: str,
    addons_paths: list[Path],
) -> str:
    """Generate the contents of a ``.odoo-dev-mcp.toml`` config file.

    Paths are written relative to ``root_path`` where possible.
    """
    root_path = root_path.resolve()

    def _rel(p: Path) -> str:
        p = p.resolve()
        try:
            return "./" + str(p.relative_to(root_path))
        except ValueError:
            return str(p)

    lines: list[str] = [
        "# OdooDevMCP project configuration",
        "# The knowledge-graph index is stored in .odoo-dev-mcp/index.db",
        "# Add   .odoo-dev-mcp/   to your .gitignore",
        "",
        "[project]",
        f'name = "{name}"',
        "",
    ]
    for ap in addons_paths:
        lines += [
            "[[addons_path]]",
            f'path = "{_rel(ap)}"',
            'label = ""',
            "",
        ]
    lines += [
        "[index]",
        'exclude_patterns = ["*/test_*", "*/demo_data"]',
        "js_parsing = false",
        "",
        "[watch]",
        "enabled = true",
        "debounce_ms = 250",
        "",
    ]
    return "\n".join(lines)
