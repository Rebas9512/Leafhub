"""
LeafHub management server — FastAPI app for Web UI configuration.

Startup: load master key, open SQLite DB, mount admin routers.
Shutdown: close DB connection.

Bind address: 127.0.0.1 (loopback only — never exposed to the network).
Serve Vue SPA from ui/dist/ if present.

Usage (from CLI):
    leafhub manage [--port 8765]

Ref: ModelHub/server.py (lifespan stripped of scheduler / usage_writer)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Request must be importable at module level so FastAPI can resolve
# the annotation `request: Request` in route functions defined inside
# create_app() (with __future__.annotations, annotations are lazy strings
# resolved against module globals, not the enclosing function scope).
try:
    from fastapi import Request
except ImportError:
    Request = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    from leafhub.core.crypto import load_master_key
    from leafhub.core.db import default_hub_dir, open_db
    from leafhub.core.store import SyncStore

    # hub_dir is set by create_app(); use None to get default (~/.leafhub/)
    resolved = app.state.hub_dir
    default  = default_hub_dir()
    hub_arg  = None if resolved == default else resolved

    # master_key may be preset by create_app() (for tests / programmatic use)
    preset_key = getattr(app.state, "_preset_master_key", None)
    master_key = preset_key if preset_key is not None else load_master_key(hub_arg)

    conn  = open_db(hub_arg)
    store = SyncStore(conn)

    app.state.master_key     = master_key
    app.state.store          = store
    app.state.oauth_sessions = {}   # session_id → {status, provider, error}

    log.info("LeafHub manage server ready — storage at %s", resolved)
    yield

    store.close()
    log.info("LeafHub manage server stopped.")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(hub_dir: Path | None = None,
               master_key: bytes | None = None) -> "fastapi.FastAPI":
    """
    Build and return the FastAPI app.

    Args:
        hub_dir:    Override storage directory (None → ~/.leafhub/).
                    Mainly used in tests.
        master_key: Pre-supply the master key (skips env/keychain/file lookup).
                    Mainly used in tests to avoid touching ~/.leafhub/.masterkey.
    """
    try:
        from fastapi import Depends, FastAPI, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        raise ImportError(
            "Web UI dependencies not installed. "
            "Run: pip install 'leafhub[manage]'"
        ) from None

    from leafhub.core.db import default_hub_dir
    from leafhub.manage.auth import verify_admin_token
    from leafhub.manage.providers import router as providers_router
    from leafhub.manage.projects import router as projects_router

    resolved_dir = hub_dir if hub_dir is not None else default_hub_dir()

    app = FastAPI(
        title="LeafHub",
        description="Local encrypted API key vault — management UI",
        version="0.1.0",
        docs_url="/admin/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.hub_dir            = resolved_dir
    app.state._preset_master_key = master_key   # None → load in lifespan

    # CORS: allow same-host origins (Vite dev server + production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8765",
            "http://127.0.0.1:8765",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── System endpoints ──────────────────────────────────────────────────

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    _admin_dep = [Depends(verify_admin_token)]

    @app.get("/admin/status", tags=["system"], dependencies=_admin_dep)
    async def admin_status(request: Request):
        import asyncio
        store = request.app.state.store
        providers = await asyncio.to_thread(store.list_providers)
        projects  = await asyncio.to_thread(store.list_projects)
        return {
            "storage_dir":     str(resolved_dir),
            "providers":       len(providers),
            "projects":        len(projects),
            "active_projects": sum(1 for p in projects if p.is_active),
        }

    # ── Admin routers (token-protected when LEAFHUB_ADMIN_TOKEN is set) ───

    app.include_router(
        providers_router,
        prefix="/admin",
        tags=["admin-providers"],
        dependencies=_admin_dep,
    )
    app.include_router(
        projects_router,
        prefix="/admin",
        tags=["admin-projects"],
        dependencies=_admin_dep,
    )

    # ── Root info (no UI built) ────────────────────────────────────────────

    _ui_dist = Path(__file__).parent.parent.parent.parent / "ui" / "dist"
    if not _ui_dist.exists():
        @app.get("/", include_in_schema=False)
        async def root_info():
            return {
                "message": "LeafHub manage server",
                "docs":    "/admin/docs",
                "health":  "/health",
                "note":    "Web UI not built — run 'npm run build' in ui/",
            }

    # ── Serve Vue UI (production build) ───────────────────────────────────

    if _ui_dist.exists():
        # Vite content-hashes asset filenames (index-Abc123.js), so assets
        # can be cached aggressively.  index.html itself must never be cached
        # because it references those hashed filenames — browsers must always
        # fetch the latest index.html to discover the current asset URLs.
        # Using no-cache (revalidate) instead of no-store keeps ETags working
        # so unchanged responses still return 304 (no extra bandwidth).
        class _NoCacheStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope: dict):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-cache"
                return response

        _assets_dir = _ui_dist / "assets"
        if _assets_dir.exists():
            app.mount(
                "/assets",
                _NoCacheStaticFiles(directory=str(_assets_dir)),
                name="ui-assets",
            )

        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str = ""):
            if path.startswith(("admin/", "health", "assets/")):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            return FileResponse(
                str(_ui_dist / "index.html"),
                headers={"Cache-Control": "no-cache"},
            )

    return app


# ── Server launcher (called by CLI) ───────────────────────────────────────────

def run_server(
    port: int = 8765,
    host: str = "127.0.0.1",
    hub_dir: Path | None = None,
    reload: bool = False,
) -> None:
    """
    Start the Uvicorn server.  Blocks until Ctrl+C.

    Args:
        port:    TCP port (default 8765).
        host:    Bind address (default 127.0.0.1 — loopback only).
        hub_dir: Override storage directory (None → ~/.leafhub/).
        reload:  Enable auto-reload (development only).
    """
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "uvicorn is not installed. "
            "Run: pip install 'leafhub[manage]'"
        ) from None

    # Build the app with the resolved hub_dir before passing to uvicorn.
    # When reload=False we pass the app object directly (no import string needed).
    app = create_app(hub_dir=hub_dir)

    log.info("Starting LeafHub manage server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, reload=reload, log_config=None)
