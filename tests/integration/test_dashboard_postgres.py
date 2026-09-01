from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text

PUBLIC_URL = os.environ.get("PHASE2D_PUBLIC_URL")
DATABASE_URL = os.environ.get("PHASE2D_DATABASE_URL")
SEED_PASSWORD = os.environ.get("MICROLENS_SEED_PASSWORD")

pytestmark = pytest.mark.skipif(
    not PUBLIC_URL or not DATABASE_URL or not SEED_PASSWORD,
    reason="set PHASE2D_PUBLIC_URL/PHASE2D_DATABASE_URL/seed password for PostgreSQL E2E",
)


def test_dashboard_overview_reconciles_with_postgresql_before_and_after_change() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    item_id = f"phase2d-offline-{uuid.uuid4().hex}"
    window = {"from_utc": "2026-08-31T00:00:00Z", "to_utc": "2026-09-02T00:00:00Z"}
    with httpx.Client(base_url=PUBLIC_URL, timeout=10.0) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "operator_readonly", "password": SEED_PASSWORD},
        )
        assert login.status_code == 200

        def assert_reconciled() -> dict[str, object]:
            response = client.get("/api/admin/dashboard/overview", params=window)
            assert response.status_code == 200, response.text
            dashboard = response.json()
            with engine.connect() as connection:
                sql = connection.execute(
                    text(
                        "SELECT "
                        "count(*) FILTER (WHERE role = 'user' AND status = 'enabled'), "
                        "(SELECT count(*) FROM recommendation_requests WHERE created_at >= :a "
                        "AND created_at < :b), "
                        "(SELECT count(*) FROM exposures WHERE exposed_at >= :a "
                        "AND exposed_at < :b), "
                        "(SELECT count(*) FROM items WHERE online_status = 'offline') "
                        "FROM users"
                    ),
                    {"a": window["from_utc"], "b": window["to_utc"]},
                ).one()
            assert dashboard["total_users"] == sql[0]
            assert dashboard["requests"] == sql[1]
            assert dashboard["exposures"] == sql[2]
            assert dashboard["offline_item_count"] == sql[3]
            return dashboard

        before = assert_reconciled()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO items "
                        "(id,title,metadata_status,online_status,state_version,updated_at) "
                        "VALUES (:id,'Phase 2D offline fixture','complete','offline',0,now())"
                    ),
                    {"id": item_id},
                )
            after = assert_reconciled()
            assert after["offline_item_count"] == before["offline_item_count"] + 1
        finally:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM items WHERE id = :id"), {"id": item_id})
    engine.dispose()
