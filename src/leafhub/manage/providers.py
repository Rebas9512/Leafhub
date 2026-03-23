"""
Admin API — Provider management.

Endpoints:
  GET    /admin/providers
  POST   /admin/providers
  GET    /admin/providers/{id}
  PUT    /admin/providers/{id}
  DELETE /admin/providers/{id}

Ref: ModelHub/admin/providers.py
     (validate/discover/reset-cooldown removed — Leafhub has no scheduler)
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter()


# ── Provider connectivity probe ───────────────────────────────────────────────

# Lightweight GET endpoints that reveal auth/connectivity status quickly.
# Lightweight endpoint probed for each API format.
#
# openai-completions  — base URL always includes /v1 (e.g. .../v1)
#                       → append /models → .../v1/models  ✓
#
# anthropic-messages  — base URL does NOT include /v1 (e.g. https://api.anthropic.com
#                       or https://api.minimax.io/anthropic)
#                       → append /v1/models → .../v1/models  ✓
#
# ollama              — preset base URL is http://localhost:11434/v1 (OpenAI-compat)
#                       → append /models → .../v1/models  ✓
#                       (native /api/tags lives at the root, not under /v1)
_PROBE_PATH: dict[str, str] = {
    "openai-completions": "/models",
    "openai-responses":   "/models",
    "anthropic-messages": "/v1/models",
    "ollama":             "/models",
}


def _probe_provider(
    base_url:      str,
    api_format:    str,
    api_key:       str,
    auth_mode:     str,
    auth_header:   str | None,
    extra_headers: dict[str, str],
    *,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """
    Make a single lightweight GET request to verify provider connectivity.

    Returns ``(ok, message)`` — never raises.

    Acceptance policy:
      2xx           → OK
      401 / 403     → endpoint reached, credentials rejected
      404           → wrong base URL
      429           → rate-limited (key works, endpoint reachable) → OK
      other 4xx/5xx → endpoint reachable → OK (server-side issue, not config)
      network error → not reachable
    """
    if auth_mode != "none" and not api_key:
        return False, "API key is required for this auth mode"

    path = _PROBE_PATH.get(api_format, "/models")
    url  = base_url.rstrip("/") + path

    headers: dict[str, str] = {}

    if auth_mode == "bearer" and api_key:
        name = auth_header or "Authorization"
        headers[name] = f"Bearer {api_key}"
    elif auth_mode == "x-api-key" and api_key:
        name = auth_header or "x-api-key"
        headers[name] = api_key

    # Anthropic requires the API version header on every request.
    if api_format == "anthropic-messages" and "anthropic-version" not in extra_headers:
        headers.setdefault("anthropic-version", "2023-06-01")

    headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status: int = resp.status
            if 200 <= status < 300:
                return True, f"Connected successfully (HTTP {status})"
            # Shouldn't normally land here (4xx/5xx raise HTTPError), but be safe.
            return False, f"Unexpected response from server (HTTP {status})"

    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 401:
            return False, "Authentication failed — check your API key (HTTP 401)"
        if code == 403:
            return False, (
                "Access denied — API key may lack permissions (HTTP 403)"
            )
        if code == 404:
            # anthropic-messages providers often don't implement GET /v1/models
            # (it's optional in the Anthropic spec).  Treat 404 as reachable for
            # this format; a truly wrong base URL would produce a connection error.
            if api_format == "anthropic-messages":
                return True, f"Endpoint reachable (model listing not required, HTTP 404)"
            return False, (
                f"Endpoint not found — verify Base URL (probed: {url}, HTTP 404)"
            )
        if code == 429:
            # Rate-limited means the key is valid and the endpoint is reachable.
            return True, f"Rate-limited but endpoint reachable (HTTP {code})"
        if 400 <= code < 500:
            # Other 4xx: endpoint is reachable, likely a format/version mismatch
            # that the user's actual runtime calls will handle.
            return True, f"Endpoint reachable (HTTP {code})"
        if code >= 500:
            return True, f"Endpoint reachable — server returned HTTP {code}"
        return False, f"HTTP error {code}"

    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        return False, f"Connection failed: {reason}"

    except TimeoutError:
        return False, f"Connection timed out after {timeout:.0f}s — check base URL"

    except Exception as exc:  # noqa: BLE001
        return False, f"Probe error: {exc}"


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProviderCreateRequest(BaseModel):
    label:            str
    provider_type:    str = "custom"
    api_format:       str = "openai-completions"
    base_url:         str
    default_model:    str
    api_key:          str = ""
    available_models: list[str] = Field(default_factory=list)
    # auth_mode: inferred from api_format when omitted
    #   bearer    → Authorization: Bearer <key>  (OpenAI, most providers)
    #   x-api-key → x-api-key: <key>             (Anthropic)
    #   none      → no auth header               (local Ollama)
    auth_mode:        str | None            = None
    auth_header:      str | None            = None
    extra_headers:    dict[str, str]        = Field(default_factory=dict)


class ProviderUpdateRequest(BaseModel):
    # provider_type and api_format are intentionally immutable after creation.
    # To change them, delete and recreate the provider.
    label:            str | None            = None
    base_url:         str | None            = None
    default_model:    str | None            = None
    api_key:          str | None            = None   # set to update the key
    available_models: list[str] | None      = None
    auth_mode:        str | None            = None
    auth_header:      str | None            = None
    # extra_headers: None = don't change; {} = clear; {k:v} = replace entirely
    extra_headers:    dict[str, str] | None = None


def _store(request: Request):
    return request.app.state.store


def _master_key(request: Request) -> bytes:
    return request.app.state.master_key


def _hub_dir(request: Request):
    return request.app.state.hub_dir


def _provider_dict(p) -> dict:
    return {
        "id":                p.id,
        "label":             p.label,
        "provider_type":     p.provider_type,
        "api_format":        p.api_format,
        "base_url":          p.base_url,
        "default_model":     p.default_model,
        "available_models":  p.available_models,
        "auth_mode":         p.auth_mode,
        "auth_header":       p.auth_header,
        "extra_headers":     p.extra_headers,
        "oauth_account_id":  p.oauth_account_id,
        "created_at":        p.created_at,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/providers")
async def list_providers(request: Request):
    store = _store(request)
    providers = await asyncio.to_thread(store.list_providers)
    return {"data": [_provider_dict(p) for p in providers]}


@router.post("/providers", status_code=201)
async def create_provider(request: Request, body: ProviderCreateRequest):
    from leafhub.core.crypto import decrypt_providers, encrypt_providers
    from leafhub.core.store import SUPPORTED_API_FORMATS, SUPPORTED_AUTH_MODES

    if body.api_format not in SUPPORTED_API_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported api_format '{body.api_format}'. "
                   f"Must be one of: {sorted(SUPPORTED_API_FORMATS)}",
        )
    if body.auth_mode is not None and body.auth_mode not in SUPPORTED_AUTH_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported auth_mode '{body.auth_mode}'. "
                   f"Must be one of: {sorted(SUPPORTED_AUTH_MODES)}",
        )

    from leafhub.core.store import DEFAULT_AUTH_MODE

    store      = _store(request)
    master_key = _master_key(request)
    hub_dir    = _hub_dir(request)

    # Probe connectivity before persisting anything.
    resolved_auth_mode = body.auth_mode or DEFAULT_AUTH_MODE.get(body.api_format, "bearer")
    ok, probe_msg = await asyncio.to_thread(
        _probe_provider,
        body.base_url,
        body.api_format,
        body.api_key,
        resolved_auth_mode,
        body.auth_header,
        body.extra_headers,
    )
    if not ok:
        raise HTTPException(
            status_code=422,
            detail=f"Provider connectivity check failed: {probe_msg}",
        )

    # Reject duplicate label
    existing = await asyncio.to_thread(store.find_provider_by_label, body.label)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Provider '{body.label}' already exists",
        )

    def _create():
        return store.create_provider(
            label=body.label,
            provider_type=body.provider_type,
            api_format=body.api_format,
            base_url=body.base_url,
            default_model=body.default_model,
            available_models=body.available_models,
            auth_mode=body.auth_mode,
            auth_header=body.auth_header,
            extra_headers=body.extra_headers,
        )

    provider = await asyncio.to_thread(_create)

    # Persist API key.  If this fails, roll back the DB row so the store
    # never has a provider record without a corresponding key in providers.enc.
    def _save_key():
        key_store = decrypt_providers(master_key, hub_dir)
        key_store[provider.id] = {"api_key": body.api_key}
        encrypt_providers(key_store, master_key, hub_dir)

    try:
        await asyncio.to_thread(_save_key)
    except Exception as exc:
        await asyncio.to_thread(store.delete_provider, provider.id)
        log.error("Failed to save API key for provider %s — DB row rolled back: %s",
                  provider.id, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to encrypt and save the API key. Provider was not created.",
        ) from exc

    return _provider_dict(provider)


@router.get("/providers/{provider_id}")
async def get_provider(request: Request, provider_id: str):
    store = _store(request)
    try:
        p = await asyncio.to_thread(store.get_provider, provider_id)
    except KeyError:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    return _provider_dict(p)


@router.put("/providers/{provider_id}")
async def update_provider(request: Request, provider_id: str,
                           body: ProviderUpdateRequest):
    from leafhub.core.crypto import decrypt_providers, encrypt_providers

    store      = _store(request)
    master_key = _master_key(request)
    hub_dir    = _hub_dir(request)

    # Verify exists
    try:
        await asyncio.to_thread(store.get_provider, provider_id)
    except KeyError:
        raise HTTPException(404, f"Provider '{provider_id}' not found")

    def _update():
        return store.update_provider(
            provider_id,
            label=body.label,
            base_url=body.base_url,
            default_model=body.default_model,
            available_models=body.available_models,
            auth_mode=body.auth_mode,
            auth_header=body.auth_header,
            extra_headers=body.extra_headers,
        )

    p = await asyncio.to_thread(_update)

    # Update API key if provided
    if body.api_key is not None:
        def _update_key():
            key_store = decrypt_providers(master_key, hub_dir)
            key_store[provider_id] = {"api_key": body.api_key}
            encrypt_providers(key_store, master_key, hub_dir)

        await asyncio.to_thread(_update_key)

    return _provider_dict(p)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(request: Request, provider_id: str):
    from leafhub.core.crypto import decrypt_providers, encrypt_providers

    store      = _store(request)
    master_key = _master_key(request)
    hub_dir    = _hub_dir(request)

    try:
        await asyncio.to_thread(store.get_provider, provider_id)
    except KeyError:
        raise HTTPException(404, f"Provider '{provider_id}' not found")

    try:
        await asyncio.to_thread(store.delete_provider, provider_id)
    except Exception as exc:
        raise HTTPException(
            409,
            f"Cannot delete provider — unbind from all projects first: {exc}",
        )

    # Remove API key from providers.enc
    def _remove_key():
        key_store = decrypt_providers(master_key, hub_dir)
        key_store.pop(provider_id, None)
        encrypt_providers(key_store, master_key, hub_dir)

    await asyncio.to_thread(_remove_key)


# ── OAuth 2.0 PKCE flow (OpenAI Codex subscription) ──────────────────────────
#
# Flow:
#   1. POST /admin/providers/oauth/start  → {session_id, auth_url}
#      Backend starts a temporary HTTP server on localhost:1455 to receive
#      the OAuth callback; returns the authorization URL to the frontend.
#   2. Frontend opens auth_url in a new browser tab.
#      User signs in with their ChatGPT account.
#   3. OpenAI redirects to localhost:1455/auth/callback with ?code=...
#      Backend exchanges code for tokens and saves the provider.
#   4. GET /admin/providers/oauth/status/{session_id}
#      Frontend polls until status is "done" or "error".


class OAuthStartRequest(BaseModel):
    label:         str
    default_model: str = "gpt-5.4"


async def _run_oauth_background(
    *,
    app,
    session_id: str,
    state:      str,
    verifier:   str,
    label:      str,
    default_model: str,
    existing_id: str | None,
) -> None:
    """
    Background asyncio task:
      • Waits for the OAuth callback on localhost:1455
      • Exchanges the code for tokens
      • Creates (or re-authenticates) the provider
      • Updates the session state so the poll endpoint can return the result
    """
    from leafhub.core.crypto import decrypt_providers, encrypt_providers
    from leafhub.core.oauth import CODEX_BASE_URL, exchange_code, get_account_id

    sessions   = app.state.oauth_sessions
    store      = app.state.store
    master_key = app.state.master_key
    hub_dir    = app.state.hub_dir

    _code:  list[str | None] = [None]
    _error: list[str | None] = [None]
    _done  = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs     = urllib.parse.parse_qs(parsed.query)

            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return

            got_state = qs.get("state", [None])[0]
            if got_state != state:
                _error[0] = "State mismatch — possible CSRF attempt."
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>State mismatch. Return to LeafHub and try again.</h2>")
                _done.set()
                return

            code = qs.get("code", [None])[0]
            if not code:
                _error[0] = "Missing authorization code in callback."
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Missing code. Return to LeafHub and try again.</h2>")
                _done.set()
                return

            _code[0] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;padding:2rem'>"
                b"<h2>Authentication complete.</h2>"
                b"<p>You may close this tab and return to LeafHub.</p>"
                b"</body></html>"
            )
            _done.set()

        def log_message(self, *_):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", 1455), _Handler)
    except OSError as exc:
        sessions[session_id]["status"] = "error"
        sessions[session_id]["error"]  = (
            f"Cannot bind localhost:1455 for OAuth callback ({exc}). "
            "Check that no other OAuth session is already in progress."
        )
        return

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        got_code = await asyncio.to_thread(_done.wait, 300)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    if _error[0]:
        sessions[session_id]["status"] = "error"
        sessions[session_id]["error"]  = _error[0]
        return

    if not got_code or not _code[0]:
        sessions[session_id]["status"] = "error"
        sessions[session_id]["error"]  = "OAuth timed out after 5 minutes."
        return

    try:
        tokens     = await asyncio.to_thread(exchange_code, _code[0], verifier)
        account_id = get_account_id(tokens["access_token"])

        if existing_id:
            provider = await asyncio.to_thread(
                store.update_provider,
                existing_id,
                default_model=default_model,
                oauth_account_id=account_id,
            )
        else:
            provider = await asyncio.to_thread(
                store.create_provider,
                label=label,
                provider_type="openai",
                api_format="openai-responses",
                base_url=CODEX_BASE_URL,
                default_model=default_model,
                available_models=[default_model],
                auth_mode="openai-oauth",
                oauth_account_id=account_id,
            )

        def _save():
            key_store = decrypt_providers(master_key, hub_dir)
            key_store[provider.id] = {
                "api_key":      tokens["refresh_token"],
                "access_token": tokens["access_token"],
                "expires_ms":   tokens["expires_ms"],
            }
            encrypt_providers(key_store, master_key, hub_dir)

        await asyncio.to_thread(_save)

        sessions[session_id]["status"]   = "done"
        sessions[session_id]["provider"] = _provider_dict(provider)

    except Exception as exc:
        log.exception("OAuth provider save failed for session %s", session_id)
        sessions[session_id]["status"] = "error"
        sessions[session_id]["error"]  = str(exc)


@router.post("/providers/oauth/start", status_code=202)
async def start_oauth_provider(request: Request, body: OAuthStartRequest):
    """
    Initiate the OpenAI Codex OAuth PKCE flow.

    Returns ``{session_id, auth_url}``.  The caller should open ``auth_url``
    in a browser tab and then poll ``/admin/providers/oauth/status/{session_id}``
    until status is ``"done"`` or ``"error"``.
    """
    from leafhub.core.oauth import (
        _CLIENT_ID, _AUTHORIZE_URL, _REDIRECT_URI, _SCOPE, _pkce_pair,
    )

    store = _store(request)

    existing = await asyncio.to_thread(store.find_provider_by_label, body.label)
    if existing and existing.auth_mode != "openai-oauth":
        raise HTTPException(
            409,
            f"Provider '{body.label}' already exists with auth_mode='{existing.auth_mode}'. "
            "Use a different label for the OAuth provider.",
        )

    verifier, challenge = _pkce_pair()
    state      = secrets.token_hex(16)
    session_id = secrets.token_hex(16)

    params = {
        "response_type":              "code",
        "client_id":                  _CLIENT_ID,
        "redirect_uri":               _REDIRECT_URI,
        "scope":                      _SCOPE,
        "code_challenge":             challenge,
        "code_challenge_method":      "S256",
        "state":                      state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow":  "true",
    }
    auth_url = _AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    sessions = request.app.state.oauth_sessions

    # Guard: reject if another OAuth session is already in progress
    # (only one callback server can bind port 1455 at a time).
    for _sid, _sess in sessions.items():
        if _sess.get("status") == "pending":
            raise HTTPException(
                409,
                "Another OAuth session is already in progress. "
                "Complete or cancel it before starting a new one.",
            )

    # Evict completed/errored sessions to prevent unbounded memory growth.
    stale = [k for k, v in sessions.items() if v.get("status") in ("done", "error")]
    for k in stale:
        del sessions[k]

    sessions[session_id] = {"status": "pending", "provider": None, "error": None}

    asyncio.create_task(_run_oauth_background(
        app=request.app,
        session_id=session_id,
        state=state,
        verifier=verifier,
        label=body.label,
        default_model=body.default_model,
        existing_id=existing.id if existing else None,
    ))

    return {"session_id": session_id, "auth_url": auth_url}


@router.get("/providers/oauth/status/{session_id}")
async def oauth_provider_status(request: Request, session_id: str):
    """
    Poll for the result of an OAuth flow started by ``/admin/providers/oauth/start``.

    Returns ``{status, provider, error}`` where status is one of:
      • ``"pending"`` — waiting for the user to complete sign-in
      • ``"done"``    — provider created/updated; ``provider`` contains the data
      • ``"error"``   — something went wrong; ``error`` contains the message
    """
    sessions = request.app.state.oauth_sessions
    session  = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "OAuth session not found or expired.")
    return session
