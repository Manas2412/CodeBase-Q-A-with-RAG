"""Tests for the shared-password auth flow.

Covers the auth-helper module directly (no HTTP) and the endpoints via
FastAPI's TestClient. The pattern intentionally uses `monkeypatch.setenv`
so we can flip the configured password between tests without touching a
real `.env` file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


# ── Helper module ──────────────────────────────────────────────────────
def test_is_configured_reflects_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert auth.is_configured() is False

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    assert auth.is_configured() is True


def test_verify_password_returns_false_when_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    # Even if the caller provides the "right" candidate, an unconfigured
    # backend must never accept it — else a fresh install with no
    # DASHBOARD_PASSWORD would be an open door.
    assert auth.verify_password("") is False
    assert auth.verify_password("anything") is False


def test_verify_password_matches_correct_password(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse battery staple")
    assert auth.verify_password("correct horse battery staple") is True
    assert auth.verify_password("wrong") is False
    assert auth.verify_password("") is False


# ── HTTP layer ─────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient with a known password + a dev session secret.

    TestClient preserves cookies within a single instance, so a login
    call sets the session cookie that subsequent calls carry — this is
    exactly the shape a real browser produces.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "swordfish")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-prod")
    # TestClient invocations skip the lifespan when not used as a context
    # manager, so the DB pool never opens — perfect for auth-only tests.
    return TestClient(app)


def test_me_reports_unauthenticated_by_default(client: TestClient) -> None:
    r = client.get("/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["configured"] is True


def test_login_success_sets_session_and_me_flips(client: TestClient) -> None:
    r = client.post("/auth/login", json={"password": "swordfish"})
    assert r.status_code == 200
    assert r.json() == {"authenticated": True, "configured": True}

    # The cookie is now on the client — /auth/me sees the session
    r = client.get("/auth/me")
    assert r.json()["authenticated"] is True


def test_login_failure_returns_401_and_does_not_authenticate(client: TestClient) -> None:
    r = client.post("/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid password"

    # Still unauthenticated on /me
    r = client.get("/auth/me")
    assert r.json()["authenticated"] is False


def test_logout_clears_session(client: TestClient) -> None:
    client.post("/auth/login", json={"password": "swordfish"})
    assert client.get("/auth/me").json()["authenticated"] is True

    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False

    # And /me now reflects logged-out state
    assert client.get("/auth/me").json()["authenticated"] is False


def test_protected_endpoint_returns_401_when_unauthenticated(client: TestClient) -> None:
    # Any of the /projects family — we pick the list one because it's the
    # cheapest and doesn't touch the DB before the auth check.
    r = client.get("/projects")
    assert r.status_code == 401
    assert r.json()["detail"] == "Not authenticated"


def test_protected_endpoint_reachable_after_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After login, /projects gets past auth. It may fail on DB access
    (no pool wired) but must NOT return 401. We stub the DB with a
    lightweight fake for this test."""
    from contextlib import asynccontextmanager
    from app import main as main_module

    class _FakeConn:
        async def fetch(self, *a, **kw):
            return []
        async def fetchval(self, *a, **kw):
            return 0

    class _FakePool:
        def acquire(self):
            @asynccontextmanager
            async def _cm():
                yield _FakeConn()
            return _cm()

    monkeypatch.setattr(main_module, "db_pool", _FakePool())

    # Log in
    client.post("/auth/login", json={"password": "swordfish"})

    # Now the protected route is reachable
    r = client.get("/projects")
    assert r.status_code == 200


def test_auth_endpoints_public_even_when_unauthed(client: TestClient) -> None:
    """/auth/login, /auth/logout, /auth/me must never require a session
    — otherwise the frontend can't log the user in at all."""
    assert client.get("/auth/me").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    # Wrong password still gets a 401 from login (that's a business rule),
    # but not a 401 from the auth dependency itself.
    r = client.post("/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
