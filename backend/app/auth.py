"""Shared-password session auth for the dashboard.

Model
=====

Phase 1 is an internal-only tool. Everyone with the password has the
same access. There's no per-user identity yet — a single
`DASHBOARD_PASSWORD` env var gates the whole UI.

  • `POST /auth/login`   { password } → sets a signed session cookie
  • `POST /auth/logout`                → clears the cookie
  • `GET  /auth/me`                    → reports whether the caller is authed

`require_auth` is a FastAPI dependency you attach to every protected
route. Public routes (the three under `/auth/*`) skip it.

Cookies are signed via `SESSION_SECRET`. Rotating that secret invalidates
every active session — the intended way to force logout across the team
after a suspected leak.

Why not JWT / per-user
----------------------

Phase 1 is behind a VPN / private network. Once we open the surface to
external users OR need audit trails per person, we promote to a small
users table + password hashes. That refactor is additive; nothing here
prevents it.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from fastapi import HTTPException, Request, Response

#: The session cookie's key inside starlette's SessionMiddleware. Change
#: it and every currently-signed cookie stops matching — same effect as
#: rotating SESSION_SECRET but scoped to the app layer.
SESSION_KEY: str = "auth"

#: How long a session lives without activity. 14 days is a compromise:
#: long enough that day-to-day use doesn't ask for the password on every
#: browser restart, short enough that a stolen laptop's cookie stops
#: working within a week or two.
SESSION_MAX_AGE_SECONDS: int = 14 * 24 * 60 * 60

#: Env vars — declared as constants so shell + test overrides go through
#: `os.environ` and there's a single grep target if we need to rename.
ENV_DASHBOARD_PASSWORD: str = "DASHBOARD_PASSWORD"
ENV_SESSION_SECRET: str = "SESSION_SECRET"


def _expected_password_hash() -> str:
    """SHA-256 of the current `DASHBOARD_PASSWORD` env var.

    Comparing hashes rather than raw strings avoids leaking the password
    length through timing on `secrets.compare_digest`. In practice both
    are fine for a shared password behind a VPN, but the hash form also
    lets us swap in an env-supplied hash (`DASHBOARD_PASSWORD_HASH`)
    later without a code change to the call sites.
    """
    pw = os.getenv(ENV_DASHBOARD_PASSWORD, "")
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _hash_candidate(candidate: str) -> str:
    return hashlib.sha256((candidate or "").encode("utf-8")).hexdigest()


def is_configured() -> bool:
    """True when a non-empty DASHBOARD_PASSWORD is set."""
    return bool(os.getenv(ENV_DASHBOARD_PASSWORD, "").strip())


def verify_password(candidate: str) -> bool:
    """Constant-time compare of hashes. False if no password configured."""
    if not is_configured():
        return False
    return hmac.compare_digest(_expected_password_hash(), _hash_candidate(candidate))


def sign_in(request: Request) -> None:
    """Mark the current session as authenticated."""
    request.session[SESSION_KEY] = True


def sign_out(request: Request) -> None:
    """Drop the auth marker AND flush the session so all keys go with it."""
    request.session.pop(SESSION_KEY, None)
    # Belt + braces — starlette's session dict clear-on-empty semantics
    # aren't documented as a guarantee, so we make the intent explicit.
    request.session.clear()


def is_signed_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def require_auth(request: Request) -> None:
    """FastAPI dependency — attach to every protected route.

    Returns None on success (the value is unused; FastAPI dependencies
    just need to not raise). Raises 401 otherwise, which the frontend
    catches to redirect to /login.
    """
    if not is_signed_in(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
