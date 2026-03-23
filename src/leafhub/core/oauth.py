"""
OpenAI Codex OAuth 2.0 — Authorization Code + PKCE flow.

Uses OpenAI's public client_id (same as the official Codex CLI).
Tokens are stored encrypted in providers.enc via the existing crypto layer.

Ref: @mariozechner/pi-ai/dist/utils/oauth/openai-codex.js
"""

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
from typing import TypedDict

log = logging.getLogger(__name__)

_CLIENT_ID     = "app_EMoamEEZ73f0CkXaXp7hrann"
_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL     = "https://auth.openai.com/oauth/token"
_REDIRECT_URI  = "http://localhost:1455/auth/callback"
_SCOPE         = "openid profile email offline_access"
_EXPIRE_BUFFER_S = 300  # refresh 5 min before actual expiry

# Default endpoint and model for subscription-based Codex calls.
# Unlike api.openai.com, this endpoint applies ChatGPT subscription quota.
CODEX_BASE_URL      = "https://chatgpt.com/backend-api/codex/responses"
CODEX_DEFAULT_MODEL = "gpt-5.4"

# All models available through the ChatGPT OAuth / Codex endpoint.
# Sourced from openclaw extensions/openai/openai-codex-provider.ts
CODEX_MODELS = [
    "gpt-5.4",              # Latest — 1M context, 128K max output (recommended)
    "gpt-5.3-codex-spark",  # Reasoning model — 128K context
    "gpt-5.3-codex",        # Previous gen standard
    "gpt-5.1-codex-mini",   # Fast & lightweight (alias: codex-mini)
    "gpt-5.2-codex",        # Older generation
]


class OAuthTokens(TypedDict):
    access_token:  str
    refresh_token: str
    expires_ms:    int  # epoch milliseconds


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge). challenge = base64url(SHA256(verifier))."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def get_account_id(access_token: str) -> str | None:
    """Extract chatgpt_account_id from the JWT payload."""
    payload = _decode_jwt(access_token)
    auth = payload.get("https://api.openai.com/auth", {})
    aid = auth.get("chatgpt_account_id")
    return aid if isinstance(aid, str) and aid else None


# ── Token exchange ────────────────────────────────────────────────────────────

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def exchange_code(code: str, verifier: str) -> OAuthTokens:
    """Exchange authorization code + PKCE verifier for tokens."""
    data = _post_form(_TOKEN_URL, {
        "grant_type":    "authorization_code",
        "client_id":     _CLIENT_ID,
        "code":          code,
        "code_verifier": verifier,
        "redirect_uri":  _REDIRECT_URI,
    })
    return {
        "access_token":  data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_ms":    int(time.time() * 1000) + int(data["expires_in"]) * 1000,
    }


def refresh_access_token(refresh_token: str) -> OAuthTokens:
    """Get a fresh access_token from a stored refresh_token."""
    data = _post_form(_TOKEN_URL, {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     _CLIENT_ID,
    })
    return {
        "access_token":  data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_ms":    int(time.time() * 1000) + int(data["expires_in"]) * 1000,
    }


def is_token_fresh(expires_ms: int) -> bool:
    """True if the access_token has more than 5 minutes of life left."""
    return expires_ms > int(time.time() * 1000) + _EXPIRE_BUFFER_S * 1000


# ── Browser PKCE flow ─────────────────────────────────────────────────────────

def run_pkce_flow() -> tuple[OAuthTokens, str | None]:
    """
    Run the full Authorization Code + PKCE flow.

    Steps:
      1. Bind a local HTTP server on 127.0.0.1:1455 for the callback.
      2. Open the browser (or print URL for headless environments).
      3. Wait up to 5 minutes for the redirect with the authorization code.
      4. Exchange code for tokens.

    Returns:
        (OAuthTokens, account_id) — account_id may be None if the JWT
        does not contain a chatgpt_account_id claim.

    Raises:
        RuntimeError on port conflict, OAuth error, or timeout.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_hex(16)

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

    _code:  list[str | None] = [None]
    _error: list[str | None] = [None]
    _done  = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs     = urllib.parse.parse_qs(parsed.query)

            if parsed.path != "/auth/callback":
                self._reply(404, b"<h2>Not found</h2>")
                return

            got_state = qs.get("state", [None])[0]
            if got_state != state:
                _error[0] = "State mismatch — possible CSRF. Please try again."
                self._reply(400, b"<h2>State mismatch. Close this and try again.</h2>")
                _done.set()
                return

            code = qs.get("code", [None])[0]
            if not code:
                _error[0] = "Missing authorization code in callback."
                self._reply(400, b"<h2>Missing code. Close this and try again.</h2>")
                _done.set()
                return

            _code[0] = code
            self._reply(
                200,
                b"<html><body style='font-family:sans-serif;padding:2rem'>"
                b"<h2>Authentication complete.</h2>"
                b"<p>You may close this window and return to the terminal.</p>"
                b"</body></html>",
            )
            _done.set()

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # suppress request logging

    try:
        server = http.server.HTTPServer(("127.0.0.1", 1455), _Handler)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot bind localhost:1455 for the OAuth callback ({exc}).\n"
            "Make sure no other process is using port 1455 and try again."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    import webbrowser
    print("\nOpening browser for OpenAI authentication...")
    print(f"  If the browser does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    try:
        _done.wait(timeout=300)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    if _error[0]:
        raise RuntimeError(f"OAuth error: {_error[0]}")
    if not _code[0]:
        raise RuntimeError(
            "OAuth timed out — no authorization code received within 5 minutes."
        )

    tokens     = exchange_code(_code[0], verifier)
    account_id = get_account_id(tokens["access_token"])
    return tokens, account_id
