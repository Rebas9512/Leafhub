"""
SyncStore — synchronous CRUD over SQLite.

Read path  : used by LeafHub SDK (authenticate, resolve_binding, get_provider)
Write path : used by CLI (Phase 2) and manage server (Phase 3)

Security:
  - Tokens stored as SHA-256 hashes only; plaintext never persisted.
  - hmac.compare_digest used for constant-time token comparison.

Ref: ModelHub/core/project_registry.py + ModelHub/core/provider_registry.py
     (merged, stripped of async/aiosqlite, scheduler fields removed)
"""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_TOKEN_PREFIX     = "lh-proj-"
_TOKEN_RAND_BYTES = 16   # → 32 hex chars; full token = "lh-proj-" + 32 = 40 chars

SUPPORTED_API_FORMATS = frozenset({
    "openai-completions",
    "openai-responses",   # OpenAI Responses API (used by ChatGPT Codex OAuth endpoint)
    "anthropic-messages",
    "ollama",
})

SUPPORTED_AUTH_MODES = frozenset({
    "bearer",        # Authorization: Bearer <api_key>  (OpenAI, Ollama, most providers)
    "x-api-key",     # x-api-key: <api_key>             (Anthropic)
    "none",          # no auth header                   (local Ollama without auth)
    "openai-oauth",  # ChatGPT OAuth token (auto-refreshed via refresh_token)
})

# Inferred auth_mode when none is specified, keyed by api_format.
DEFAULT_AUTH_MODE: dict[str, str] = {
    "openai-completions": "bearer",
    "openai-responses":   "bearer",
    "anthropic-messages": "x-api-key",
    "ollama":             "none",
}
# Keep the private alias for internal backwards compatibility.
_DEFAULT_AUTH_MODE = DEFAULT_AUTH_MODE


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Provider:
    id:               str
    label:            str
    provider_type:    str
    api_format:       str
    base_url:         str
    default_model:    str
    available_models: list[str]
    created_at:       str
    # Authentication metadata — tells callers how to inject the API key.
    # auth_mode:   "bearer" | "x-api-key" | "none"
    # auth_header: override the default header name (None = use mode default)
    # extra_headers: additional fixed headers required by this provider
    #   e.g. {"anthropic-version": "2023-06-01", "OpenAI-Organization": "org-xx"}
    auth_mode:        str                = "bearer"
    auth_header:      str | None         = None
    extra_headers:    dict[str, str]     = field(default_factory=dict)
    # Populated only for auth_mode="openai-oauth" — the ChatGPT account ID
    # extracted from the JWT after login.  Stored in plaintext (not sensitive).
    oauth_account_id: str | None         = None


@dataclass
class ModelBinding:
    id:             str
    project_id:     str
    alias:          str
    provider_id:    str
    model_override: str | None


@dataclass
class Project:
    id:           str
    name:         str
    token_prefix: str   # e.g. "lh-proj-a1b2" — safe to display in UI
    is_active:    bool
    created_at:   str
    bindings:     list[ModelBinding] = field(default_factory=list)
    path:         str | None         = None   # linked project directory (written by link endpoint)


# ── Token helpers ─────────────────────────────────────────────────────────────

def _hash_token(raw_token: str) -> str:
    """SHA-256 of the raw token (hex digest)."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def generate_token() -> str:
    """Generate a new project token: lh-proj-<32 hex chars>."""
    return _TOKEN_PREFIX + secrets.token_hex(_TOKEN_RAND_BYTES)


# ── SyncStore ─────────────────────────────────────────────────────────────────

class SyncStore:
    """
    Synchronous SQLite store.

    Instantiate with an open sqlite3.Connection from core.db.open_db():

        conn  = open_db()          # or open_db(hub_dir=Path("/tmp/test"))
        store = SyncStore(conn)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ─────────────────────────────────────────────────────────────────────
    # Providers
    # ─────────────────────────────────────────────────────────────────────

    def create_provider(
        self,
        label:            str,
        provider_type:    str,
        api_format:       str,
        base_url:         str,
        default_model:    str,
        available_models:  list[str] | None     = None,
        auth_mode:         str | None           = None,
        auth_header:       str | None           = None,
        extra_headers:     dict[str, str] | None = None,
        oauth_account_id:  str | None           = None,
    ) -> Provider:
        """
        Insert a provider row. API key is NOT stored here — it goes into
        providers.enc via core.crypto.encrypt_providers().

        auth_mode defaults to the canonical mode for api_format when omitted:
          openai-completions → bearer
          anthropic-messages → x-api-key
          ollama             → none
        """
        if api_format not in SUPPORTED_API_FORMATS:
            raise ValueError(
                f"Unsupported api_format '{api_format}'. "
                f"Must be one of: {sorted(SUPPORTED_API_FORMATS)}"
            )

        resolved_auth_mode = auth_mode or _DEFAULT_AUTH_MODE.get(api_format, "bearer")
        if resolved_auth_mode not in SUPPORTED_AUTH_MODES:
            raise ValueError(
                f"Unsupported auth_mode '{resolved_auth_mode}'. "
                f"Must be one of: {sorted(SUPPORTED_AUTH_MODES)}"
            )

        provider_id      = str(uuid.uuid4())
        available_json   = json.dumps(available_models or [])
        extra_headers_js = json.dumps(extra_headers or {})

        self._conn.execute(
            """
            INSERT INTO providers
                (id, label, provider_type, api_format,
                 base_url, default_model, available_models,
                 auth_mode, auth_header, extra_headers, oauth_account_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (provider_id, label, provider_type, api_format,
             base_url, default_model, available_json,
             resolved_auth_mode, auth_header, extra_headers_js, oauth_account_id),
        )
        self._conn.commit()
        log.info("Created provider '%s' (%s)", label, provider_id)
        return self.get_provider(provider_id)

    def get_provider(self, provider_id: str) -> Provider:
        row = self._conn.execute(
            "SELECT * FROM providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Provider not found: {provider_id}")
        return _row_to_provider(row)

    def list_providers(self) -> list[Provider]:
        rows = self._conn.execute(
            "SELECT * FROM providers ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_provider(r) for r in rows]

    def update_provider(
        self,
        provider_id: str,
        *,
        label:             str | None            = None,
        base_url:          str | None            = None,
        default_model:     str | None            = None,
        available_models:  list[str] | None      = None,
        auth_mode:         str | None            = None,
        auth_header:       str | None            = None,
        extra_headers:     dict[str, str] | None = None,
        oauth_account_id:  str | None            = None,
    ) -> Provider:
        """
        Update provider metadata fields.  Pass only the fields to change.

        extra_headers semantics:
          None      → don't change
          {}        → clear all extra headers
          {k: v}    → replace the entire extra_headers dict with this value
        """
        if auth_mode is not None and auth_mode not in SUPPORTED_AUTH_MODES:
            raise ValueError(
                f"Unsupported auth_mode '{auth_mode}'. "
                f"Must be one of: {sorted(SUPPORTED_AUTH_MODES)}"
            )

        sets, vals = [], []
        if label is not None:
            sets.append("label = ?");            vals.append(label)
        if base_url is not None:
            sets.append("base_url = ?");         vals.append(base_url)
        if default_model is not None:
            sets.append("default_model = ?");    vals.append(default_model)
        if available_models is not None:
            sets.append("available_models = ?"); vals.append(json.dumps(available_models))
        if auth_mode is not None:
            sets.append("auth_mode = ?");        vals.append(auth_mode)
        if auth_header is not None:
            sets.append("auth_header = ?");      vals.append(auth_header)
        if extra_headers is not None:
            sets.append("extra_headers = ?");       vals.append(json.dumps(extra_headers))
        if oauth_account_id is not None:
            sets.append("oauth_account_id = ?");    vals.append(oauth_account_id)

        if sets:
            self._conn.execute(
                f"UPDATE providers SET {', '.join(sets)} WHERE id = ?",
                (*vals, provider_id),
            )
            self._conn.commit()
        return self.get_provider(provider_id)

    def delete_provider(self, provider_id: str) -> None:
        """
        Remove provider metadata from the DB.
        NOTE: The API key in providers.enc is NOT removed here — that is the
        responsibility of the caller (CLI / manage server) which must call
        encrypt_providers() with the updated key store after deletion.
        Orphaned keys are harmless (no binding can reference a deleted provider),
        but callers should clean them up to avoid stale data accumulating.
        """
        self._conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        self._conn.commit()
        log.info("Deleted provider %s", provider_id)

    # ─────────────────────────────────────────────────────────────────────
    # Projects
    # ─────────────────────────────────────────────────────────────────────

    def create_project(self, name: str) -> tuple[Project, str]:
        """
        Create a project.
        Returns (Project, raw_token) — raw_token is shown ONCE, never recoverable.
        Ref: ModelHub/core/project_registry.py — create()
        """
        raw_token    = generate_token()
        token_hash   = _hash_token(raw_token)
        token_prefix = raw_token[:12]           # "lh-proj-xxxx"
        project_id   = str(uuid.uuid4())

        self._conn.execute(
            "INSERT INTO projects (id, name, token_hash, token_prefix) VALUES (?,?,?,?)",
            (project_id, name, token_hash, token_prefix),
        )
        self._conn.commit()
        log.info("Created project '%s' (%s)", name, project_id)
        return self.get_project(project_id), raw_token

    def get_project(self, project_id: str) -> Project:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Project not found: {project_id}")
        return self._row_to_project(row)

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def authenticate_project(self, raw_token: str) -> Project | None:
        """
        Validate a raw token. Returns Project if valid and active, else None.
        Uses hmac.compare_digest for constant-time comparison to prevent
        timing-based token enumeration attacks.
        Ref: litellm/litellm/proxy/auth/user_api_key_auth.py — hash_token()
        """
        candidate_hash = _hash_token(raw_token)
        row = self._conn.execute(
            "SELECT * FROM projects WHERE token_hash = ? AND is_active = 1",
            (candidate_hash,),
        ).fetchone()

        if row is None:
            return None
        # Token is found by SHA-256 hash index; constant-time comparison at
        # this point is redundant (both values are the same hash), but we keep
        # hmac.compare_digest here as a clear signal to readers that token
        # comparison must never use ==.  The real timing-safe property is the
        # hash-before-lookup pattern, combined with the loopback-only bind.
        if not hmac.compare_digest(row["token_hash"], candidate_hash):
            return None  # unreachable in normal operation

        return self._row_to_project(row)

    def rename_project(self, project_id: str, name: str) -> Project:
        self._conn.execute(
            "UPDATE projects SET name = ? WHERE id = ?", (name, project_id)
        )
        self._conn.commit()
        return self.get_project(project_id)

    def rotate_token(self, project_id: str) -> str:
        """Generate a new token and invalidate the old one. Returns raw token."""
        raw_token    = generate_token()
        token_hash   = _hash_token(raw_token)
        token_prefix = raw_token[:12]

        self._conn.execute(
            "UPDATE projects SET token_hash = ?, token_prefix = ? WHERE id = ?",
            (token_hash, token_prefix, project_id),
        )
        self._conn.commit()
        log.info("Rotated token for project %s", project_id)
        return raw_token

    def deactivate_project(self, project_id: str) -> None:
        self._conn.execute(
            "UPDATE projects SET is_active = 0 WHERE id = ?", (project_id,)
        )
        self._conn.commit()

    def activate_project(self, project_id: str) -> None:
        self._conn.execute(
            "UPDATE projects SET is_active = 1 WHERE id = ?", (project_id,)
        )
        self._conn.commit()

    def delete_project(self, project_id: str) -> None:
        # model_bindings cascade-deleted by FK constraint
        self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()
        log.info("Deleted project %s", project_id)

    # ─────────────────────────────────────────────────────────────────────
    # Model bindings
    # ─────────────────────────────────────────────────────────────────────

    def add_binding(
        self,
        project_id:     str,
        alias:          str,
        provider_id:    str,
        model_override: str | None = None,
    ) -> ModelBinding:
        """Add or replace a single alias binding."""
        binding_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT OR REPLACE INTO model_bindings
                (id, project_id, alias, provider_id, model_override)
            VALUES (?,?,?,?,?)
            """,
            (binding_id, project_id, alias, provider_id, model_override),
        )
        self._conn.commit()
        return ModelBinding(
            id=binding_id,
            project_id=project_id,
            alias=alias,
            provider_id=provider_id,
            model_override=model_override,
        )

    def remove_binding(self, project_id: str, alias: str) -> None:
        self._conn.execute(
            "DELETE FROM model_bindings WHERE project_id = ? AND alias = ?",
            (project_id, alias),
        )
        self._conn.commit()

    def set_bindings(
        self,
        project_id: str,
        bindings: list[dict],   # [{alias, provider_id, model_override?}, ...]
    ) -> None:
        """Replace all bindings for a project atomically.

        Uses the connection as a context manager so any failed INSERT
        automatically rolls back the preceding DELETE.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM model_bindings WHERE project_id = ?", (project_id,)
            )
            for b in bindings:
                self._conn.execute(
                    """
                    INSERT INTO model_bindings
                        (id, project_id, alias, provider_id, model_override)
                    VALUES (?,?,?,?,?)
                    """,
                    (str(uuid.uuid4()), project_id,
                     b["alias"], b["provider_id"], b.get("model_override")),
                )

    def resolve_binding(self, project_id: str, alias: str) -> ModelBinding | None:
        """Resolve alias → ModelBinding, or None if not bound."""
        row = self._conn.execute(
            "SELECT * FROM model_bindings WHERE project_id = ? AND alias = ?",
            (project_id, alias),
        ).fetchone()
        if row is None:
            return None
        return _row_to_binding(row)

    def list_bindings(self, project_id: str) -> list[ModelBinding]:
        rows = self._conn.execute(
            "SELECT * FROM model_bindings WHERE project_id = ? ORDER BY alias",
            (project_id,),
        ).fetchall()
        return [_row_to_binding(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────────
    # Lookup helpers (for CLI)
    # ─────────────────────────────────────────────────────────────────────

    def find_provider_by_label(self, label: str) -> Provider | None:
        """Return the first provider with the given label, or None."""
        row = self._conn.execute(
            "SELECT * FROM providers WHERE label = ? LIMIT 1", (label,)
        ).fetchone()
        return _row_to_provider(row) if row else None

    def find_project_by_name(self, name: str) -> Project | None:
        """Return the first project with the given name, or None."""
        row = self._conn.execute(
            "SELECT * FROM projects WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return self._row_to_project(row) if row else None

    def find_project_by_path(self, path: str) -> Project | None:
        """Return the active project linked to *path*, or None.

        Path-based dedup is preferred over name-based dedup because a single
        project directory may be registered under different names (e.g. when
        multiple agents share one codebase).
        """
        row = self._conn.execute(
            "SELECT * FROM projects WHERE path = ? AND is_active = 1 LIMIT 1",
            (path,),
        ).fetchone()
        return self._row_to_project(row) if row else None

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # ─────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────

    def set_project_path(self, project_id: str, path: str | None) -> Project:
        """Store the linked filesystem path for a project (None to unlink)."""
        self._conn.execute(
            "UPDATE projects SET path = ? WHERE id = ?", (path, project_id)
        )
        self._conn.commit()
        return self.get_project(project_id)

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        bindings = self.list_bindings(row["id"])
        # path column may be absent in databases opened before this migration.
        # sqlite3.Row raises IndexError on unknown column names (not KeyError).
        # Catch both for safety across Python / SQLite version variations.
        try:
            path = row["path"]
        except (IndexError, KeyError):
            path = None
        return Project(
            id=row["id"],
            name=row["name"],
            token_prefix=row["token_prefix"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            bindings=bindings,
            path=path,
        )


# ── Module-level row converters (no self needed) ──────────────────────────────

def _row_to_provider(row: sqlite3.Row) -> Provider:
    try:
        oauth_account_id = row["oauth_account_id"]
    except (IndexError, KeyError):
        oauth_account_id = None
    return Provider(
        id=row["id"],
        label=row["label"],
        provider_type=row["provider_type"],
        api_format=row["api_format"],
        base_url=row["base_url"],
        default_model=row["default_model"],
        available_models=json.loads(row["available_models"] or "[]"),
        created_at=row["created_at"],
        auth_mode=row["auth_mode"] or "bearer",
        auth_header=row["auth_header"],
        extra_headers=json.loads(row["extra_headers"] or "{}"),
        oauth_account_id=oauth_account_id,
    )


def _row_to_binding(row: sqlite3.Row) -> ModelBinding:
    return ModelBinding(
        id=row["id"],
        project_id=row["project_id"],
        alias=row["alias"],
        provider_id=row["provider_id"],
        model_override=row["model_override"],
    )
