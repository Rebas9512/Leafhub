"""
Admin API authentication.

Reads LEAFHUB_ADMIN_TOKEN from environment at import time.
If not set: logs a warning and allows all requests (dev/localhost mode).
If set:     requires `Authorization: Bearer <token>` with constant-time
            comparison and per-IP failure rate limiting.

Rate limit: 5 failures / 5 min → locked out for 5 min.

Ref: ModelHub/admin/auth.py (adapted — env var renamed, proxy dep removed)
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

_RATE_LIMIT  = 5      # max failures before lockout
_RATE_WINDOW = 300.0  # 5-minute sliding window (and lockout duration)


# ── In-memory rate limiter ─────────────────────────────────────────────────────

class _RateLimiter:
    """Per-IP sliding-window failure counter with timed lockout."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit  = limit
        self._window = window
        # {ip: [timestamp, ...]}
        self._failures: dict[str, list[float]] = defaultdict(list)

    def _prune(self, ip: str) -> None:
        cutoff = time.monotonic() - self._window
        self._failures[ip] = [t for t in self._failures[ip] if t > cutoff]

    def is_blocked(self, ip: str) -> bool:
        self._prune(ip)
        return len(self._failures[ip]) >= self._limit

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.monotonic())

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)


_limiter = _RateLimiter(limit=_RATE_LIMIT, window=_RATE_WINDOW)


def _reset_limiter_for_tests() -> None:
    """Clear all rate-limit state.  Call in test teardown to prevent leak."""
    _limiter._failures.clear()


def _get_client_ip(request: Request) -> str:
    # The server binds exclusively to 127.0.0.1, so only local processes can
    # connect.  Trusting X-Forwarded-For would let any local caller spoof its
    # IP and bypass the rate limiter, so we use the real transport address only.
    if request.client:
        return request.client.host
    return "unknown"


# ── FastAPI dependency ─────────────────────────────────────────────────────────

def verify_admin_token(request: Request) -> None:
    """
    FastAPI dependency injected into every admin router.

    Dev mode  — LEAFHUB_ADMIN_TOKEN not set: all requests pass.
    Prod mode — requires Authorization: Bearer <token> checked with
                hmac.compare_digest (constant-time, no timing oracle).

    After _RATE_LIMIT failures within _RATE_WINDOW the IP is locked out.

    Token is read from os.environ on every call so runtime env changes and
    test-time patches are visible without module reloads.
    """
    admin_token: str | None = os.environ.get("LEAFHUB_ADMIN_TOKEN") or None

    if not admin_token:
        log.debug("LEAFHUB_ADMIN_TOKEN not set — admin endpoints unprotected")
        return  # dev mode — loopback bind is the only required guard

    client_ip = _get_client_ip(request)

    if _limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed auth attempts — try again later",
        )

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        _limiter.record_failure(client_ip)
        raise HTTPException(status_code=401, detail="Admin token required")

    provided = auth[7:].strip()
    if not hmac.compare_digest(provided.encode(), admin_token.encode()):
        _limiter.record_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid admin token")

    _limiter.record_success(client_ip)
