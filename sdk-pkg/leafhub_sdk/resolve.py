"""
Unified credential resolution for LeafHub consumer projects.

Supports three usage modes::

    # Mode A: get a ResolvedCredential object
    cred = resolve("llm")
    print(cred.api_key, cred.base_url, cred.model)

    # Mode B: get an env-var dict for injection (Trileaf style)
    env = resolve("rewrite", as_env=True)
    os.environ.update(env)

    # Mode C: get a pre-built client (LeafScan style)
    cred = resolve("llm", as_client=True)
    client = cred.client  # openai.OpenAI or anthropic.Anthropic

Resolution priority:
  1. LeafHub vault (.leafhub token -> SDK -> get_config)
  2. env_fallbacks declared in leafhub.toml
  3. Common provider env vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
  4. Raise CredentialError with actionable message
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import Binding, Manifest, find_manifest, load_manifest
from .probe import ProbeResult, detect

log = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────────

class CredentialError(Exception):
    """Raised when credential resolution fails for all available paths."""


# ── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_AUTH_HEADER: dict[str, str] = {
    "bearer": "Authorization",
    "x-api-key": "x-api-key",
}


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class ResolvedCredential:
    """
    Resolved credential ready for use by consumer projects.

    Fields match LeafHub's ProviderConfig plus metadata about the resolution
    source.  The ``client`` field is only populated when ``as_client=True``.
    """
    api_key:       str
    base_url:      str = ""
    model:         str = ""
    api_format:    str = ""          # openai-completions | anthropic-messages | ollama | openai-responses
    auth_mode:     str = "bearer"    # bearer | x-api-key | none
    auth_header:   str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    source:        str = ""          # "leafhub" | "env" | "env-fallback:<VAR_NAME>"
    client:        Any = None        # populated only when as_client=True

    def build_headers(self) -> dict[str, str]:
        """Build HTTP headers with auth, matching LeafHub ProviderConfig."""
        headers: dict[str, str] = {}
        if self.auth_mode != "none" and self.api_key:
            name = self.auth_header or _DEFAULT_AUTH_HEADER.get(
                self.auth_mode, "Authorization"
            )
            value = (
                f"Bearer {self.api_key}"
                if self.auth_mode == "bearer"
                else self.api_key
            )
            headers[name] = value
        headers.update(self.extra_headers)
        return headers


# ── Well-known provider env vars ─────────────────────────────────────────────

_COMMON_ENV_VARS: list[tuple[str, str, str]] = [
    # (env_var, api_format, default_model)
    ("ANTHROPIC_API_KEY",  "anthropic-messages",  "claude-sonnet-4-6"),
    ("OPENAI_API_KEY",     "openai-completions",  "gpt-4o"),
    ("GROQ_API_KEY",       "openai-completions",  "llama-3.3-70b-versatile"),
    ("MISTRAL_API_KEY",    "openai-completions",  "mistral-large-latest"),
    ("XAI_API_KEY",        "openai-completions",  "grok-2"),
    ("TOGETHER_API_KEY",   "openai-completions",  "meta-llama/Llama-3-70b-chat-hf"),
    ("OPENROUTER_API_KEY", "openai-completions",  "openai/gpt-4o"),
    ("GEMINI_API_KEY",     "openai-completions",  "gemini-1.5-pro"),
]

_BASE_URLS: dict[str, str] = {
    "GROQ_API_KEY":       "https://api.groq.com/openai/v1",
    "MISTRAL_API_KEY":    "https://api.mistral.ai/v1",
    "XAI_API_KEY":        "https://api.x.ai/v1",
    "TOGETHER_API_KEY":   "https://api.together.xyz/v1",
    "OPENROUTER_API_KEY": "https://openrouter.ai/api/v1",
    "GEMINI_API_KEY":     "https://generativelanguage.googleapis.com/v1beta/openai",
}


# ── Public API ───────────────────────────────────────────────────────────────

def resolve(
    alias: str | None = None,
    *,
    project_dir: Path | str | None = None,
    as_env: bool = False,
    as_client: bool = False,
) -> ResolvedCredential | dict[str, str]:
    """
    Resolve credentials for a LeafHub alias.

    Args:
        alias:       Alias to resolve. If None, reads default from leafhub.toml.
        project_dir: Project directory to search from (default: cwd).
        as_env:      If True, return a dict of ``{PREFIX}_*`` env vars instead.
        as_client:   If True, populate ``cred.client`` with a ready API client.

    Returns:
        A :class:`ResolvedCredential` (default), or a dict (when ``as_env=True``).

    Raises:
        CredentialError: All resolution paths failed.
    """
    project_root = Path(project_dir or Path.cwd()).resolve()

    # Load manifest (optional — resolve still works without one)
    manifest = _try_load_manifest(project_root)

    # Resolve the alias
    if alias is None:
        if manifest and manifest.default_alias():
            alias = manifest.default_alias()
        else:
            raise CredentialError(
                "No alias specified and no leafhub.toml found with a default binding.\n"
                "Pass an alias explicitly:  resolve('my-alias')"
            )

    # Look up binding config from manifest
    binding = manifest.get_binding(alias) if manifest else None

    # Try resolution paths in order
    cred = (
        _try_leafhub(alias, project_root)
        or _try_env_fallbacks(alias, manifest)
        or _try_common_env_vars()
    )

    if cred is None:
        _raise_credential_error(alias, binding, manifest)
        assert False, "unreachable"  # _raise_credential_error always raises

    # Build client if requested
    if as_client and cred.client is None:
        cred.client = _build_client(cred)

    # Return as env dict if requested
    if as_env:
        return _to_env_dict(cred, binding)

    return cred


# ── Resolution strategies ────────────────────────────────────────────────────

def _try_leafhub(alias: str, project_dir: Path) -> ResolvedCredential | None:
    """Strategy 1: resolve from LeafHub vault via .leafhub token."""
    probe_result = detect(project_dir=project_dir)
    if not probe_result.ready:
        return None

    try:
        hub = probe_result.open_sdk()
        cfg = hub.get_config(alias)
        return ResolvedCredential(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=cfg.model,
            api_format=cfg.api_format,
            auth_mode=cfg.auth_mode,
            auth_header=cfg.auth_header,
            extra_headers=cfg.extra_headers,
            source="leafhub",
        )
    except ImportError:
        log.debug("LeafHub SDK not installed — skipping vault resolution")
        return None
    except Exception as exc:
        _name = type(exc).__name__
        if "InvalidToken" in _name:
            log.warning(
                "LeafHub token is invalid or expired. "
                "Re-link with:  leafhub project link <name> --path ."
            )
        elif "AliasNotBound" in _name:
            log.warning(
                "LeafHub project is linked but alias '%s' has no binding. "
                "Bind with:  leafhub project bind <project> --alias %s --provider <name>",
                alias, alias,
            )
        else:
            log.debug("LeafHub resolution failed: %s: %s", _name, exc)
        return None


def _try_env_fallbacks(
    alias: str, manifest: Manifest | None
) -> ResolvedCredential | None:
    """Strategy 2: check env vars declared in leafhub.toml [env_fallbacks]."""
    if manifest is None:
        return None

    fallback_vars = manifest.env_fallbacks.get(alias, [])
    for var_name in fallback_vars:
        value = os.environ.get(var_name)
        if value:
            fmt, model, base_url = _infer_format_from_env_var(var_name)
            return ResolvedCredential(
                api_key=value,
                base_url=base_url,
                model=model,
                api_format=fmt,
                source=f"env-fallback:{var_name}",
            )
    return None


def _try_common_env_vars() -> ResolvedCredential | None:
    """Strategy 3: check well-known provider env vars."""
    for var_name, fmt, default_model in _COMMON_ENV_VARS:
        value = os.environ.get(var_name)
        if value:
            base_url = _BASE_URLS.get(var_name, "")
            return ResolvedCredential(
                api_key=value,
                base_url=base_url,
                model=default_model,
                api_format=fmt,
                source=f"env:{var_name}",
            )
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _try_load_manifest(project_dir: Path) -> Manifest | None:
    """Load manifest without raising (returns None on failure)."""
    try:
        manifest_path = find_manifest(project_dir)
        if manifest_path is None:
            return None
        return load_manifest(manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        log.debug("Could not load leafhub.toml: %s", exc)
        return None


def _infer_format_from_env_var(
    var_name: str,
) -> tuple[str, str, str]:
    """Infer api_format, default_model, and base_url from an env var name."""
    for known_var, fmt, default_model in _COMMON_ENV_VARS:
        if var_name == known_var:
            base_url = _BASE_URLS.get(var_name, "")
            return fmt, default_model, base_url
    # Unknown var — default to openai-compatible
    return "openai-completions", "", ""


def _to_env_dict(
    cred: ResolvedCredential, binding: Binding | None
) -> dict[str, str]:
    """Convert ResolvedCredential to a dict of PREFIX_* env vars."""
    prefix = (binding.env_prefix if binding and binding.env_prefix else "").rstrip("_")

    if not prefix:
        # No prefix declared — use a generic mapping
        result: dict[str, str] = {}
        if cred.api_key:
            result["API_KEY"] = cred.api_key
        if cred.base_url:
            result["BASE_URL"] = cred.base_url
        if cred.model:
            result["MODEL"] = cred.model
        if cred.api_format:
            result["API_KIND"] = cred.api_format
        if cred.auth_mode:
            result["AUTH_MODE"] = cred.auth_mode
        if cred.auth_header:
            result["AUTH_HEADER"] = cred.auth_header
        if cred.source:
            result["CREDENTIAL_SOURCE"] = cred.source
        return result

    result = {}
    if cred.api_key:
        result[f"{prefix}_API_KEY"] = cred.api_key
    if cred.base_url:
        result[f"{prefix}_BASE_URL"] = cred.base_url
    if cred.model:
        result[f"{prefix}_MODEL"] = cred.model
    if cred.api_format:
        result[f"{prefix}_API_KIND"] = cred.api_format
    if cred.auth_mode:
        result[f"{prefix}_AUTH_MODE"] = cred.auth_mode
    if cred.auth_header:
        result[f"{prefix}_AUTH_HEADER"] = cred.auth_header
    if cred.source:
        result[f"{prefix}_CREDENTIAL_SOURCE"] = cred.source
    return result


def _build_client(cred: ResolvedCredential) -> Any:
    """Build an API client based on api_format."""
    fmt = cred.api_format

    if fmt == "anthropic-messages":
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic is not installed. Run: pip install anthropic"
            ) from None
        return anthropic.Anthropic(
            api_key=cred.api_key,
            base_url=cred.base_url or None,
        )

    if fmt in ("openai-completions", "ollama", "openai-responses"):
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai is not installed. Run: pip install openai"
            ) from None

        base_url = cred.base_url
        # For openai-responses, strip trailing /responses to avoid
        # .../responses/responses (OpenAI SDK appends it automatically)
        if fmt == "openai-responses" and base_url:
            base = base_url.rstrip("/")
            if base.endswith("/responses"):
                base_url = base[: -len("/responses")]

        return openai.OpenAI(
            api_key=cred.api_key,
            base_url=base_url or None,
        )

    # Unknown format — return None, let the consumer handle it
    log.warning("Unknown api_format '%s' — cannot build client", fmt)
    return None


def _raise_credential_error(
    alias: str,
    binding: Binding | None,
    manifest: Manifest | None,
) -> None:
    """Raise a CredentialError with an actionable message."""
    lines = [f"Could not resolve credentials for alias '{alias}'."]
    lines.append("")
    lines.append("Tried (in order):")
    lines.append("  1. LeafHub vault (.leafhub token)")

    if manifest and alias in manifest.env_fallbacks:
        vars_str = ", ".join(manifest.env_fallbacks[alias])
        lines.append(f"  2. Manifest env_fallbacks: {vars_str}")

    lines.append("  3. Common provider env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)")
    lines.append("")
    lines.append("To fix:")
    lines.append("  - Run:  leafhub register .   (link this project to LeafHub)")
    lines.append(f"  - Or set one of the env vars listed above")

    if binding and binding.env_prefix:
        lines.append(
            f"  - Or set:  {binding.env_prefix}_API_KEY=<your-key>"
        )

    raise CredentialError("\n".join(lines))
