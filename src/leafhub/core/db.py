"""
SQLite database initialisation — synchronous, stdlib only.

Schema vs ModelHub:
  - Removed: usage_logs table
  - Removed: rpm_limit, tpm_limit, validation_status, validated_at  (providers)
  - Removed: rpm_limit, max_concurrent, token_expires_at, last_seen_at (projects)
  - Added:   hub_dir parameter on all path helpers (supports testing)
  - Added:   auth_mode, auth_header, extra_headers on providers (v0.2 migration)

Used by both the SDK (read-only) and the CLI/manage layer (read-write).
The manage server (Phase 3) wraps synchronous calls in asyncio.to_thread() rather
than using aiosqlite, keeping a single synchronous code path across all layers.
"""

import logging
import re
import sqlite3
from pathlib import Path

from leafhub.core import default_hub_dir  # canonical definition lives here

log = logging.getLogger(__name__)

_SCHEMA_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous  = NORMAL",
    "PRAGMA cache_size   = -8000",
    "PRAGMA temp_store   = MEMORY",
    "PRAGMA foreign_keys = ON",
]

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS providers (
    id               TEXT PRIMARY KEY,
    label            TEXT NOT NULL UNIQUE,
    provider_type    TEXT NOT NULL,
    api_format       TEXT NOT NULL,
    base_url         TEXT NOT NULL,
    default_model    TEXT NOT NULL,
    available_models TEXT,
    auth_mode        TEXT NOT NULL DEFAULT 'bearer',
    auth_header      TEXT,
    extra_headers    TEXT,
    oauth_account_id TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    path         TEXT
);

CREATE TABLE IF NOT EXISTS model_bindings (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    alias          TEXT NOT NULL,
    provider_id    TEXT NOT NULL REFERENCES providers(id),
    model_override TEXT,
    UNIQUE(project_id, alias)
);
"""

# Columns added after initial schema — applied to existing databases on open.
_PROVIDER_MIGRATIONS: list[tuple[str, str]] = [
    ("auth_mode",        "TEXT NOT NULL DEFAULT 'bearer'"),
    ("auth_header",      "TEXT"),
    ("extra_headers",    "TEXT"),
    ("oauth_account_id", "TEXT"),
]

_PROJECT_MIGRATIONS: list[tuple[str, str]] = [
    ("path", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Bring existing databases up to the current schema, idempotently.

    1. ADD COLUMN migrations (providers and projects).
    2. Table-recreation migration: remove the historical UNIQUE constraint from
       projects.name so that same-name projects with independent tokens are allowed.
    """
    existing_providers = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
    for col, definition in _PROVIDER_MIGRATIONS:
        if col not in existing_providers:
            conn.execute(f"ALTER TABLE providers ADD COLUMN {col} {definition}")
            log.info("Migrated providers table: added column '%s'", col)

    existing_projects = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    for col, definition in _PROJECT_MIGRATIONS:
        if col not in existing_projects:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {definition}")
            log.info("Migrated projects table: added column '%s'", col)

    conn.commit()

    # ── Structural migration: drop UNIQUE on projects.name ────────────────────
    # SQLite does not support DROP CONSTRAINT, so we recreate the table.
    # Only needed when the old schema (name TEXT NOT NULL UNIQUE) is detected.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if row and re.search(r"name\s+TEXT\s+NOT\s+NULL\s+UNIQUE", row[0]):
        log.info("Migrating projects table: removing UNIQUE constraint from 'name'")
        try:
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                BEGIN;
                ALTER TABLE projects RENAME TO _projects_old;
                CREATE TABLE projects (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    token_hash   TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    is_active    INTEGER NOT NULL DEFAULT 1,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    path         TEXT
                );
                INSERT INTO projects SELECT * FROM _projects_old;
                DROP TABLE _projects_old;
                COMMIT;
            """)
        finally:
            # executescript() disables foreign_keys at the start and does not
            # restore it — re-enable unconditionally so subsequent operations
            # always run with FK enforcement, even if the migration failed.
            conn.execute("PRAGMA foreign_keys = ON")


def db_path(hub_dir: Path | None = None) -> Path:
    return (hub_dir if hub_dir is not None else default_hub_dir()) / "projects.db"


def open_db(hub_dir: Path | None = None) -> sqlite3.Connection:
    """
    Open (or create) the SQLite DB, apply schema and migrations, return connection.
    Row factory set to sqlite3.Row for dict-style access.
    """
    path = db_path(hub_dir)
    # check_same_thread=False: safe because WAL mode serialises writes
    # and the manage server wraps calls in asyncio.to_thread() anyway.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    for pragma in _SCHEMA_PRAGMAS:
        conn.execute(pragma)

    # executescript runs outside a transaction; commit after to be explicit
    conn.executescript(_SCHEMA_DDL)
    conn.commit()

    # Bring existing databases up to the current column set
    _migrate(conn)

    log.debug("Database ready at %s", path)
    return conn
