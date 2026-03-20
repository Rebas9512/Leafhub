"""
LeafHub SDK — direct file-based API key access.

No server required. Reads from ~/.leafhub/ directly, decrypts in-process.

Typical usage:
    from leafhub import LeafHub

    hub    = LeafHub(token="lh-proj-xxx")
    key    = hub.get_key("gpt-4")          # raw API key string
    config = hub.get_config("gpt-4")       # ProviderConfig dataclass
    client = hub.openai("gpt-4")           # openai.OpenAI instance
    client = hub.async_openai("gpt-4")     # openai.AsyncOpenAI instance
    client = hub.anthropic("claude")       # anthropic.Anthropic instance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    AliasNotBoundError,
    DecryptionError,
    InvalidTokenError,
    StorageNotFoundError,
)

log = logging.getLogger(__name__)


# Default HTTP header name for each auth_mode.
_DEFAULT_AUTH_HEADER: dict[str, str] = {
    "bearer":    "Authorization",
    "x-api-key": "x-api-key",
}


@dataclass
class ProviderConfig:
    """
    Complete provider configuration returned to the caller.

    Intended usage:
        cfg    = hub.get_config("gpt-4")
        client = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers=cfg.build_headers(),
        )

    build_headers() merges the authentication header with extra_headers so
    callers never have to handle auth logic themselves.
    """
    api_key:       str
    base_url:      str
    model:         str               # resolved: model_override or provider.default_model
    api_format:    str               # "openai-completions" | "anthropic-messages" | "ollama"
    auth_mode:     str               = "bearer"   # "bearer" | "x-api-key" | "none"
    auth_header:   str | None        = None        # override default header name
    extra_headers: dict[str, str]    = field(default_factory=dict)

    def build_headers(self) -> dict[str, str]:
        """
        Return a complete HTTP header dict ready to pass to any HTTP client.

        Auth header logic:
          bearer    →  Authorization: Bearer <api_key>
          x-api-key →  x-api-key: <api_key>   (or auth_header name if overridden)
          none      →  (no auth header injected)

        extra_headers are merged in after auth, so they can override if needed.

        Example:
            cfg = hub.get_config("claude")
            # {"x-api-key": "sk-ant-...", "anthropic-version": "2023-06-01"}
            headers = cfg.build_headers()
        """
        headers: dict[str, str] = {}
        if self.auth_mode != "none" and self.api_key:
            name  = self.auth_header or _DEFAULT_AUTH_HEADER.get(self.auth_mode, "Authorization")
            value = f"Bearer {self.api_key}" if self.auth_mode == "bearer" else self.api_key
            headers[name] = value
        headers.update(self.extra_headers)
        return headers


class LeafHub:
    """
    Entry point for projects to access their configured API keys.

    No management server needs to be running. Reads directly from ~/.leafhub/
    (or hub_dir) and decrypts the API key within the calling process.

    The token is validated once at construction time. A LeafHub instance
    is lightweight — open one at application startup and reuse it.
    """

    def __init__(
        self,
        token:   str,
        hub_dir: str | Path | None = None,
    ) -> None:
        """
        Args:
            token:   Project token (lh-proj-...).
            hub_dir: Override the default ~/.leafhub/ path.
                     Useful for testing or non-default installations.

        Raises:
            StorageNotFoundError: Storage directory or DB does not exist.
            InvalidTokenError:    Token is invalid or project is inactive.
        """
        from .core.db import open_db, default_hub_dir
        from .core.store import SyncStore

        resolved_dir = Path(hub_dir) if hub_dir is not None else None

        # Resolve the actual path (creates ~/.leafhub/ if hub_dir is None)
        actual_dir = resolved_dir if resolved_dir is not None else default_hub_dir()
        db_file = actual_dir / "projects.db"

        if not db_file.exists():
            raise StorageNotFoundError(
                f"Leafhub storage not found at {actual_dir}. "
                "Run 'leafhub provider add' to get started, "
                "or 'leafhub manage' to open the Web UI."
            )

        conn  = open_db(resolved_dir)
        store = SyncStore(conn)

        project = store.authenticate_project(token)
        if project is None:
            conn.close()
            raise InvalidTokenError(
                "Invalid or inactive project token. "
                "Check the token or run 'leafhub project token <name>' to rotate."
            )

        self._conn    = conn
        self._store   = store
        self._project = project
        self._hub_dir = resolved_dir   # may be None (means ~/.leafhub/)

        # providers.enc is decrypted lazily on first get_key()/get_config() call
        # and cached for the lifetime of this instance.
        #
        # Hot-reload is NOT supported: if a provider's API key is updated
        # (via CLI or Web UI) after this instance is created, the cached value
        # will be stale.  Create a new LeafHub instance to pick up the change.
        self._key_cache: dict | None = None

        log.debug("LeafHub ready for project '%s' (%s)",
                  project.name, project.token_prefix)

    # ── Key access ────────────────────────────────────────────────────────

    def get_key(self, alias: str) -> str:
        """
        Return the decrypted API key string for the given alias.

        Raises:
            AliasNotBoundError: alias is not bound to this project.
            DecryptionError:    master key is wrong or providers.enc is corrupt.
        """
        return self.get_config(alias).api_key

    def get_config(self, alias: str) -> ProviderConfig:
        """
        Return a ProviderConfig (api_key, base_url, model, api_format)
        for the given alias.

        Raises:
            AliasNotBoundError: alias is not bound to this project.
            DecryptionError:    master key is wrong or providers.enc is corrupt.
        """
        binding = self._store.resolve_binding(self._project.id, alias)
        if binding is None:
            available = self.list_aliases()
            raise AliasNotBoundError(
                f"Alias '{alias}' is not bound to project '{self._project.name}'. "
                f"Available aliases: {available or '(none)'}"
            )

        provider = self._store.get_provider(binding.provider_id)

        key_store = self._load_key_store()
        entry     = key_store.get(binding.provider_id, {})
        api_key   = entry.get("api_key") or ""

        # auth_mode="none" means no API key is required (e.g. local Ollama).
        # Only raise when a key is genuinely expected but absent.
        if not api_key and provider.auth_mode != "none":
            raise DecryptionError(
                f"No API key found for provider '{provider.label}' "
                f"(id={binding.provider_id}). "
                "Re-add the provider via 'leafhub provider add' or the Web UI."
            )

        model = binding.model_override or provider.default_model
        return ProviderConfig(
            api_key=api_key,
            base_url=provider.base_url,
            model=model,
            api_format=provider.api_format,
            auth_mode=provider.auth_mode,
            auth_header=provider.auth_header,
            extra_headers=provider.extra_headers,
        )

    def list_aliases(self) -> list[str]:
        """
        Return all alias names currently bound to this project.
        Always queries the DB so the result reflects any changes made after
        this LeafHub instance was created.
        """
        return [b.alias for b in self._store.list_bindings(self._project.id)]

    # ── Convenience clients ───────────────────────────────────────────────

    def openai(self, alias: str) -> "openai.OpenAI":
        """
        Return a pre-configured openai.OpenAI instance.

        Requires: pip install openai
        Suitable for api_format: "openai-completions"
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai is not installed. Run: pip install openai"
            ) from None
        cfg = self.get_config(alias)
        return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    def async_openai(self, alias: str) -> "openai.AsyncOpenAI":
        """
        Return a pre-configured openai.AsyncOpenAI instance.

        Requires: pip install openai
        """
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "openai is not installed. Run: pip install openai"
            ) from None
        cfg = self.get_config(alias)
        return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    def anthropic(self, alias: str) -> "anthropic.Anthropic":
        """
        Return a pre-configured anthropic.Anthropic instance.

        Requires: pip install anthropic
        Suitable for api_format: "anthropic-messages"
        """
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError(
                "anthropic is not installed. Run: pip install anthropic"
            ) from None
        cfg = self.get_config(alias)
        return Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)

    # ── Context manager ───────────────────────────────────────────────────

    def __enter__(self) -> "LeafHub":
        return self

    def __exit__(self, *_: object) -> None:
        self._conn.close()

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_key_store(self) -> dict:
        """
        Decrypt providers.enc and cache the result for this instance's lifetime.
        Raises DecryptionError on failure.
        """
        from .core.crypto import decrypt_providers, load_master_key

        if self._key_cache is not None:
            return self._key_cache

        try:
            master_key      = load_master_key(self._hub_dir)
            self._key_cache = decrypt_providers(master_key, self._hub_dir)
        except RuntimeError as exc:
            raise DecryptionError(str(exc)) from exc

        return self._key_cache

    # ── Alternative constructors ───────────────────────────────────────────

    @classmethod
    def from_directory(
        cls,
        path: "str | Path | None" = None,
        hub_dir: "str | Path | None" = None,
    ) -> "LeafHub":
        """
        Auto-discover a ``.leafhub`` dotfile by walking up the directory tree
        (mirrors how git discovers ``.git/``) and return a ready instance.

        The ``.leafhub`` file is written by the LeafHub Manage UI when a user
        links a project directory, or by ``leafhub project link``.

        Args:
            path:    Directory to start searching from.  Defaults to ``Path.cwd()``.
            hub_dir: Override the default ``~/.leafhub/`` storage path.

        Raises:
            FileNotFoundError: No ``.leafhub`` file found in this or any parent
                               directory.
            InvalidTokenError: The token in the dotfile is invalid or the
                               project has been deactivated.
            StorageNotFoundError: LeafHub storage does not exist on this machine.
        """
        import json
        from pathlib import Path as _Path

        start = _Path(path or _Path.cwd()).resolve()
        for directory in [start, *start.parents]:
            dotfile = directory / ".leafhub"
            if dotfile.is_file():
                try:
                    data = json.loads(dotfile.read_text(encoding="utf-8"))
                    token = data.get("token") if isinstance(data, dict) else None
                    if token:
                        return cls(token=token, hub_dir=hub_dir)
                except (OSError, ValueError):
                    pass
                break   # found but unreadable — don't keep walking up

        raise FileNotFoundError(
            "No .leafhub file found in the current directory or any parent. "
            "Link this project from the LeafHub Manage UI, or run: "
            "leafhub project link <name> --path ."
        )

    def __del__(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
