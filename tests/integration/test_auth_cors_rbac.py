from __future__ import annotations

import os
import uuid

import httpx
import pytest

PUBLIC_URL = os.environ.get("PHASE2D_PUBLIC_URL")
SEED_PASSWORD = os.environ.get("MICROLENS_SEED_PASSWORD")
REDIS_URL = os.environ.get("PHASE2D_REDIS_URL")
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "http://localhost:5173")

pytestmark = pytest.mark.skipif(
    not PUBLIC_URL or not SEED_PASSWORD,
    reason="set PHASE2D_PUBLIC_URL and MICROLENS_SEED_PASSWORD for isolated HTTP E2E",
)

ACCOUNTS = {
    "demo_user_a": "user",
    "demo_user_b": "user",
    "demo_user_c": "user",
    "operator_readonly": "operator_readonly",
    "operator": "operator",
    "admin": "admin",
}


def _login(username: str) -> httpx.Client:
    client = httpx.Client(base_url=PUBLIC_URL, timeout=10.0)
    response = client.post(
        "/api/auth/login", json={"username": username, "password": SEED_PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == ACCOUNTS[username]
    assert client.cookies.get("microlens_session")
    assert client.cookies.get("microlens_csrf")
    return client


def test_six_seed_accounts_cookie_csrf_logout_and_four_role_matrix() -> None:
    clients = {username: _login(username) for username in ACCOUNTS}
    try:
        for username, client in clients.items():
            assert client.get("/api/auth/me").json()["username"] == username
            feed = client.get("/api/feeds/popular", params={"limit": 2})
            assert feed.status_code == 200, feed.text
            assert feed.json()["items"]
            assert feed.json()["model_version"]

        window = {"from_utc": "2026-08-31T00:00:00Z", "to_utc": "2026-09-02T00:00:00Z"}
        assert (
            clients["demo_user_a"].get("/api/admin/dashboard/overview", params=window).status_code
            == 403
        )
        for username in ("operator_readonly", "operator", "admin"):
            assert (
                clients[username].get("/api/admin/dashboard/overview", params=window).status_code
                == 200
            )
        for username in ("demo_user_a", "operator_readonly", "operator"):
            assert clients[username].get("/api/admin/users").status_code == 403
        assert clients["admin"].get("/api/admin/users").status_code == 200

        client = clients["demo_user_a"]
        stale_session = client.cookies.get("microlens_session")
        stale_csrf = client.cookies.get("microlens_csrf")
        assert client.post("/api/auth/logout").status_code == 403
        assert (
            client.post("/api/auth/logout", headers={"X-CSRF-Token": stale_csrf}).status_code == 204
        )
        stale = httpx.Client(
            base_url=PUBLIC_URL,
            cookies={"microlens_session": stale_session, "microlens_csrf": stale_csrf},
        )
        try:
            assert stale.get("/api/auth/me").status_code == 401
        finally:
            stale.close()
    finally:
        for client in clients.values():
            client.close()


def test_exact_credentialed_cors_and_public_internal_rejection() -> None:
    with httpx.Client(base_url=PUBLIC_URL, timeout=10.0) as client:
        allowed = client.options(
            "/api/auth/me",
            headers={
                "Origin": WEB_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-CSRF-Token",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == WEB_ORIGIN
        assert allowed.headers["access-control-allow-credentials"] == "true"

        denied = client.options(
            "/api/auth/me",
            headers={
                "Origin": "https://hostile.invalid",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers
        assert client.get("/internal/model-versions/secret/activate").status_code == 404


@pytest.mark.skipif(not REDIS_URL, reason="set PHASE2D_REDIS_URL to an isolated Redis database")
def test_real_redis_registration_limiter() -> None:
    from redis import Redis

    redis = Redis.from_url(REDIS_URL)
    redis.flushdb()
    prefix = uuid.uuid4().hex[:12]
    with httpx.Client(base_url=PUBLIC_URL, timeout=20.0) as client:
        statuses = [
            client.post(
                "/api/auth/register",
                json={"username": f"u_{prefix}_{index}", "password": "ValidPassphrase!42"},
            ).status_code
            for index in range(6)
        ]
    assert statuses == [201, 201, 201, 201, 201, 429]
