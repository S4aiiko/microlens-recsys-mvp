from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_extended_services_and_private_internal_listener() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {
        "web",
        "api",
        "db",
        "redis",
        "search",
        "worker",
        "scheduler",
    }
    api = compose["services"]["api"]
    assert api["ports"] == ["${API_PORT:-8000}:8000"]
    assert api["expose"] == ["8001"]
    assert api["depends_on"]["db"]["condition"] == "service_healthy"
    assert api["depends_on"]["redis"]["condition"] == "service_healthy"
    assert compose["networks"]["backend"]["internal"] is True
    assert api["environment"]["WEB_ORIGIN"] == "${WEB_ORIGIN:-http://localhost:5173}"
    assert api["environment"]["SEARCH_URL"] == "${SEARCH_URL:-http://search:9200}"
    assert "MICROLENS_SEED_PASSWORD" in api["environment"]
    search = compose["services"]["search"]
    assert "ports" not in search
    assert search["networks"] == ["backend"]
    assert search["environment"]["discovery.type"] == "single-node"


def test_makefile_honors_multiword_docker_compose_override() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "DOCKER_COMPOSE ?= docker compose" in makefile
    for target in ("smoke-all", "up", "up-core", "ps", "logs", "down"):
        body = makefile.split(f"{target}:", 1)[1].split("\n\n", 1)[0]
        assert "$(DOCKER_COMPOSE)" in body
        assert "foundation.py require-docker" not in body


@pytest.mark.skipif(
    not os.environ.get("PHASE2D_PUBLIC_URL"),
    reason="set PHASE2D_PUBLIC_URL when an isolated Compose stack is running",
)
def test_live_compose_ready_reports_postgres_alembic_redis_and_restore_boundary() -> None:
    import httpx

    response = httpx.get(f"{os.environ['PHASE2D_PUBLIC_URL']}/ready", timeout=10.0)
    assert response.status_code == 200, response.text
    checks = response.json()["checks"]
    assert checks["database"] == "ok"
    assert checks["redis"] == "ok"
    assert checks["alembic"]["at_head"] is True
    assert checks["alembic"]["current"] == checks["alembic"]["head"] == "20260902_0005"
    assert checks["active_model_restore"] in {"no_active_model", "restored"}
