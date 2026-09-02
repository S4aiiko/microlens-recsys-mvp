from __future__ import annotations

import io
import json
import unittest
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.auth.dependencies import AuthDependencies
from apps.api.app.auth.errors import install_api_error_handlers
from apps.api.app.cli.search_reindex import main as reindex_main
from apps.api.app.db.models import Role
from apps.api.app.main import create_public_app
from apps.api.app.runtime import create_runtime
from apps.api.app.search.domain import (
    READ_ALIAS,
    FullReindexResult,
    ProjectionUnavailable,
)
from apps.api.app.search.health import SearchHealthService
from apps.api.app.search.router import build_search_router
from apps.api.app.search.service import AuthoritativeSearchService
from apps.api.app.settings import AppSettings
from tests.search._support import FakeAuthority, FakeProjection, FakeRegistry, item


class Runner:
    def __init__(self, result: FullReindexResult | Exception) -> None:
        self.result = result

    def run(self, _spec: object) -> FullReindexResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SearchRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user_id = uuid.uuid4()
        self.projection = FakeProjection()
        self.projection.indices["microlens-items-v1"] = {
            "a": item("a", "Python stale", likes=30),
            "offline": item("offline", "Python offline", likes=20),
        }
        self.projection.aliases[READ_ALIAS] = ("microlens-items-v1",)
        self.authority = FakeAuthority(
            [
                item("a", "Python current", state_version=4, likes=30),
                item("offline", "Python offline", online=False, likes=20),
                item("backfill", "Python backfill", likes=10),
            ]
        )
        self.registry = FakeRegistry()

    def app(self, role: Role = Role.USER) -> FastAPI:
        async def current_user() -> object:
            return SimpleNamespace(user=SimpleNamespace(id=self.user_id, role=role))

        dependencies = AuthDependencies(
            current_user=current_user,
            csrf_user=current_user,
        )
        app = FastAPI()
        install_api_error_handlers(app)
        app.include_router(
            build_search_router(
                dependencies=dependencies,
                service=AuthoritativeSearchService(self.projection, self.authority),
                health_service=SearchHealthService(
                    self.projection,
                    self.authority,
                    self.registry,
                ),
            )
        )

        @app.get("/api/items/{item_id}")
        def dynamic_item(item_id: str) -> dict[str, str]:
            return {"item_id": item_id}

        return app

    def test_static_search_uses_current_principal_and_pg_verified_values(self) -> None:
        with TestClient(self.app()) as client:
            response = client.get("/api/items/search", params={"q": "Python", "limit": 2})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([entry["item_id"] for entry in payload["items"]], ["a", "backfill"])
        self.assertEqual(payload["items"][0]["title"], "Python current")
        self.assertNotIn("offline", [entry["item_id"] for entry in payload["items"]])
        self.assertTrue(payload["degraded"])

    def test_projection_outage_returns_explicit_degraded_pg_fallback(self) -> None:
        self.projection.reachable = False
        with TestClient(self.app()) as client:
            response = client.get("/api/items/search", params={"q": "Python", "limit": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "postgresql_fallback")
        self.assertTrue(response.json()["degraded"])

    def test_postgresql_outage_returns_503_and_never_serializes_es_documents(self) -> None:
        self.authority.fail = True
        with TestClient(self.app()) as client:
            response = client.get("/api/items/search", params={"q": "Python"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "search_authority_unavailable")
        self.assertNotIn("Python stale", response.text)
        self.assertNotIn("Python offline", response.text)

    def test_disabled_principal_maps_to_role_safe_forbidden(self) -> None:
        self.authority.denied_principals.add(self.user_id)
        with TestClient(self.app()) as client:
            response = client.get("/api/items/search", params={"q": "Python"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "search_forbidden")

    def test_health_is_operator_visible_but_not_user_visible(self) -> None:
        with TestClient(self.app(Role.OPERATOR_READONLY)) as client:
            allowed = client.get("/api/admin/search/health")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["status"], "degraded")
        self.assertEqual(
            allowed.json()["reasons"],
            ["active_index_missing_authoritative_build_record"],
        )

        with TestClient(self.app(Role.USER)) as client:
            denied = client.get("/api/admin/search/health")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "forbidden")

    def test_openapi_records_roles_authority_and_static_route(self) -> None:
        schema = self.app(Role.OPERATOR).openapi()
        search = schema["paths"]["/api/items/search"]["get"]
        self.assertEqual(
            search["x-required-roles"],
            ["user", "operator_readonly", "operator", "admin"],
        )
        self.assertTrue(search["x-postgresql-authoritative"])
        health = schema["paths"]["/api/admin/search/health"]["get"]
        self.assertEqual(
            health["x-required-roles"],
            ["operator_readonly", "operator", "admin"],
        )

        with TestClient(self.app()) as client:
            invalid = client.get("/api/items/search", params={"q": " "})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "invalid_search_query")

    def test_public_app_wires_platform_routes_and_static_search_first(self) -> None:
        settings = AppSettings.from_environment(allow_unconfigured=True)
        runtime = create_runtime(settings)
        components = SimpleNamespace(
            service=AuthoritativeSearchService(self.projection, self.authority),
            health_service=SearchHealthService(
                self.projection,
                self.authority,
                self.registry,
            ),
        )
        try:
            app = create_public_app(
                settings=settings,
                runtime=runtime,
                search_runtime=components,  # type: ignore[arg-type]
            )
            paths = app.openapi()["paths"]
            for path in (
                "/api/items/search",
                "/api/admin/search/health",
                "/api/admin/async-jobs",
                "/api/admin/operation-jobs",
                "/api/admin/alerts",
            ):
                self.assertIn(path, paths)
            route_paths: list[str] = []
            for route in app.routes:
                if hasattr(route, "path"):
                    route_paths.append(route.path)
                elif hasattr(route, "original_router"):
                    route_paths.extend(
                        nested.path
                        for nested in route.original_router.routes
                        if hasattr(nested, "path")
                    )
            self.assertLess(
                route_paths.index("/api/items/search"),
                route_paths.index("/api/items/{item_id}"),
            )
        finally:
            runtime.engine.dispose()


class SearchReindexCliTests(unittest.TestCase):
    def test_success_is_canonical_json(self) -> None:
        output = io.StringIO()
        result = FullReindexResult(
            physical_index="microlens-items-v2",
            previous_index="microlens-items-v1",
            document_count=17,
            projection_checksum="a" * 64,
            replayed=False,
        )
        code = reindex_main(
            [
                "--index-version",
                "v2",
                "--source-version",
                "data-v2",
                "--expected-current-index",
                "microlens-items-v1",
            ],
            runner=Runner(result),
            stdout=output,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["physical_index"], "microlens-items-v2")

    def test_failure_output_never_contains_transport_secret(self) -> None:
        error = io.StringIO()
        credential_marker = "credential-value"
        code = reindex_main(
            ["--index-version", "v2", "--source-version", "data-v2"],
            runner=Runner(
                ProjectionUnavailable(
                    "https://" + "test-user:" + credential_marker + "@invalid.example failed"
                )
            ),
            stderr=error,
        )
        self.assertEqual(code, 4)
        self.assertEqual(
            json.loads(error.getvalue()),
            {"status": "error", "code": "search_projection_unavailable"},
        )
        self.assertNotIn(credential_marker, error.getvalue())


if __name__ == "__main__":
    unittest.main()
