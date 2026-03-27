"""
Parse ``leafhub.toml`` project manifests.

The manifest declares a project's LeafHub bindings, setup hooks, and
environment-variable fallbacks in a single declarative file that is committed
to version control (unlike ``.leafhub`` which holds the secret token).

Minimal example::

    [project]
    name = "myapp"

    [[bindings]]
    alias = "llm"
    required = true

Full example::

    [project]
    name = "trileaf"
    python = ">=3.10"

    [[bindings]]
    alias = "rewrite"
    required = true
    env_prefix = "REWRITE"
    capabilities = ["chat"]

    [setup]
    extra_deps = ["playwright install chromium"]
    post_register = ["python -m scripts.download_models"]
    doctor_cmd = "python scripts/check_env.py"

    [env_fallbacks]
    rewrite = ["REWRITE_API_KEY", "OPENAI_API_KEY"]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── TOML parser (stdlib 3.11+ or vendored fallback) ─────────────────────────

def _load_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file. Works on Python 3.10+ (stdlib or tomli)."""
    text = path.read_text(encoding="utf-8")

    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)

    # Python 3.10 fallback: try tomli, then a minimal inline parser
    try:
        import tomli  # type: ignore[import-untyped]
        return tomli.loads(text)
    except ImportError:
        pass

    # Last resort: minimal subset parser for our specific format.
    # Handles only what leafhub.toml actually uses.
    return _minimal_toml_parse(text)


def _minimal_toml_parse(text: str) -> dict[str, Any]:
    """
    Ultra-minimal TOML subset parser — covers leafhub.toml's actual usage:
    bare keys, strings, booleans, inline arrays of strings, [tables], [[arrays]].

    NOT a general-purpose TOML parser. Only used as a last resort on Python 3.10
    when tomli is not installed.
    """
    import re
    import json

    root: dict[str, Any] = {}
    current = root
    array_table_re = re.compile(r"^\[\[([^\]]+)\]\]\s*$")
    table_re = re.compile(r"^\[([^\]]+)\]\s*$")

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # [[array.of.tables]]
        m = array_table_re.match(stripped)
        if m:
            keys = [k.strip() for k in m.group(1).split(".")]
            target = root
            for k in keys[:-1]:
                target = target.setdefault(k, {})
            arr = target.setdefault(keys[-1], [])
            new_table: dict[str, Any] = {}
            arr.append(new_table)
            current = new_table
            continue

        # [table]
        m = table_re.match(stripped)
        if m:
            keys = [k.strip() for k in m.group(1).split(".")]
            current = root
            for k in keys:
                current = current.setdefault(k, {})
            continue

        # key = value
        if "=" in stripped:
            key_part, _, val_part = stripped.partition("=")
            key = key_part.strip().strip('"')
            val_str = val_part.strip()

            if val_str.startswith("["):
                # Inline array — parse with json (close enough for string arrays)
                val = json.loads(val_str)
            elif val_str.startswith('"') and val_str.endswith('"') and len(val_str) >= 2:
                val = val_str[1:-1]
            elif val_str == "true":
                val = True
            elif val_str == "false":
                val = False
            else:
                # Try number, fall back to string
                try:
                    val = int(val_str)
                except ValueError:
                    try:
                        val = float(val_str)
                    except ValueError:
                        val = val_str

            current[key] = val

    return root


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Binding:
    """A declared alias binding in the manifest."""
    alias: str
    required: bool = False
    env_prefix: str | None = None
    capabilities: list[str] = field(default_factory=list)


@dataclass
class SetupConfig:
    """Setup hooks declared in the manifest."""
    extra_deps: list[str] = field(default_factory=list)
    post_register: list[str] = field(default_factory=list)
    doctor_cmd: str | None = None


@dataclass
class Manifest:
    """Parsed ``leafhub.toml`` manifest."""
    path: Path
    name: str
    python: str | None = None
    bindings: list[Binding] = field(default_factory=list)
    setup: SetupConfig = field(default_factory=SetupConfig)
    env_fallbacks: dict[str, list[str]] = field(default_factory=dict)

    def get_binding(self, alias: str) -> Binding | None:
        """Return the binding for *alias*, or None."""
        for b in self.bindings:
            if b.alias == alias:
                return b
        return None

    def required_aliases(self) -> list[str]:
        """Return alias names where required=true."""
        return [b.alias for b in self.bindings if b.required]

    def default_alias(self) -> str | None:
        """Return the first declared alias (convention: primary binding)."""
        return self.bindings[0].alias if self.bindings else None


# ── Loader ───────────────────────────────────────────────────────────────────

_MANIFEST_FILENAME = "leafhub.toml"


def get_default_alias(
    project_dir: Path | str | None = None,
    fallback: str = "default",
) -> str:
    """
    Read the primary alias from the nearest ``leafhub.toml``.

    Returns the first declared alias, or *fallback* if no manifest
    is found or it has no bindings.  Never raises.

    Typical usage at module level::

        from leafhub_sdk.manifest import get_default_alias
        _ALIAS = get_default_alias(project_dir=_ROOT, fallback="llm")
    """
    try:
        p = find_manifest(project_dir)
        if p is not None:
            m = load_manifest(p)
            a = m.default_alias()
            if a:
                return a
    except Exception:
        pass
    return fallback


def find_manifest(start: Path | str | None = None) -> Path | None:
    """
    Walk up from *start* looking for ``leafhub.toml``.
    Returns the absolute path, or None if not found.
    """
    directory = Path(start or Path.cwd()).resolve()
    if directory.is_file():
        directory = directory.parent

    for d in [directory, *directory.parents]:
        candidate = d / _MANIFEST_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_manifest(path: Path | str | None = None) -> Manifest:
    """
    Load and parse a ``leafhub.toml`` manifest.

    Args:
        path: Explicit path to the toml file, or a directory to search in.
              If None, searches from cwd upward.

    Raises:
        FileNotFoundError: No ``leafhub.toml`` found.
        ValueError:        Manifest is missing required fields.
    """
    if path is not None:
        p = Path(path)
        if p.is_dir():
            p = p / _MANIFEST_FILENAME
        if not p.is_file():
            raise FileNotFoundError(
                f"Manifest not found: {p}\n"
                f"Create a {_MANIFEST_FILENAME} in your project root."
            )
    else:
        p = find_manifest()
        if p is None:
            raise FileNotFoundError(
                f"No {_MANIFEST_FILENAME} found in current directory or any parent.\n"
                f"Create one in your project root."
            )

    raw = _load_toml(p)
    return _parse_manifest(raw, p)


def _parse_manifest(raw: dict[str, Any], path: Path) -> Manifest:
    """Validate and convert raw TOML dict into a Manifest."""
    project = raw.get("project", {})
    if not isinstance(project, dict):
        raise ValueError(f"{path}: [project] must be a table")

    name = project.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{path}: [project].name is required")

    python_req = project.get("python")

    # Bindings
    bindings_raw = raw.get("bindings", [])
    if not isinstance(bindings_raw, list):
        raise ValueError(f"{path}: [[bindings]] must be an array of tables")

    bindings: list[Binding] = []
    seen_aliases: set[str] = set()
    for i, b in enumerate(bindings_raw):
        if not isinstance(b, dict):
            raise ValueError(f"{path}: bindings[{i}] must be a table")
        alias = b.get("alias")
        if not alias or not isinstance(alias, str):
            raise ValueError(f"{path}: bindings[{i}].alias is required")
        if alias in seen_aliases:
            raise ValueError(f"{path}: duplicate alias '{alias}'")
        seen_aliases.add(alias)

        caps = b.get("capabilities", [])
        if isinstance(caps, str):
            caps = [caps]

        bindings.append(Binding(
            alias=alias,
            required=bool(b.get("required", False)),
            env_prefix=b.get("env_prefix"),
            capabilities=caps,
        ))

    # Setup
    setup_raw = raw.get("setup", {})
    setup = SetupConfig(
        extra_deps=setup_raw.get("extra_deps", []),
        post_register=setup_raw.get("post_register", []),
        doctor_cmd=setup_raw.get("doctor_cmd"),
    )

    # Env fallbacks
    env_fallbacks: dict[str, list[str]] = {}
    fallbacks_raw = raw.get("env_fallbacks", {})
    if isinstance(fallbacks_raw, dict):
        for alias, vars_list in fallbacks_raw.items():
            if isinstance(vars_list, list):
                env_fallbacks[alias] = [str(v) for v in vars_list]
            elif isinstance(vars_list, str):
                env_fallbacks[alias] = [vars_list]

    return Manifest(
        path=path,
        name=name,
        python=python_req,
        bindings=bindings,
        setup=setup,
        env_fallbacks=env_fallbacks,
    )
