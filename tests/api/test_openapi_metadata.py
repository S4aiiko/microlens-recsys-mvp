from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select

from apps.api.app.api.admin import DashboardQueryService, build_dashboard_router
from apps.api.app.auth import (
    AuthService,
    JWTService,
    JWTSettings,
    PasswordService,
    install_api_error_handlers,
)
from apps.api.app.auth.dependencies import build_auth_dependencies
from apps.api.app.db.models import (
    Comparability,
    EvaluationPurpose,
    ModelActivationAttempt,
    ModelStatus,
    ModelVersion,
)
from apps.api.app.db.session import session_dependency
from apps.api.app.models_registry import (
    ActivationService,
    ModelRegistryRepository,
    build_internal_activation_router,
    build_model_admin_router,
)
from apps.api.app.operations import OperationService, build_items_router, build_operations_router

from ._support import NOW, factory_for, sqlite_engine


class Loader:
    def stage(self, *, artifact_uri: str, artifact_checksum: str, manifest_checksum: str) -> object:
        return (artifact_uri, artifact_checksum, manifest_checksum)


class Runtime:
    def swap(self, *, model_version: str, staged_bundle: object) -> None:
        del model_version, staged_bundle


class RuntimeOpenAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.get_session = session_dependency(self.factory)
        auth = AuthService(
            PasswordService(),
            JWTService(JWTSettings(secret="runtime-openapi-test-secret-longer-than-32-bytes")),
        )
        self.dependencies = build_auth_dependencies(self.get_session, auth)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_public_admin_metadata_and_contract_delta_schemas(self) -> None:
        app = FastAPI(docs_url=None, redoc_url=None)
        app.include_router(
            build_dashboard_router(
                get_session=self.get_session,
                dependencies=self.dependencies,
                queries=DashboardQueryService(),
            )
        )
        app.include_router(
            build_operations_router(
                get_session=self.get_session,
                dependencies=self.dependencies,
                service=OperationService(),
            )
        )
        app.include_router(
            build_items_router(get_session=self.get_session, dependencies=self.dependencies)
        )
        app.include_router(
            build_model_admin_router(
                get_session=self.get_session,
                dependencies=self.dependencies,
                repository=ModelRegistryRepository(),
            )
        )
        schema = app.openapi()
        self.assertNotIn("/internal/model-versions/{version}/activate", schema["paths"])

        read_paths = [
            "/api/admin/dashboard/overview",
            "/api/admin/dashboard/timeseries",
            "/api/admin/dashboard/feeds",
            "/api/admin/dashboard/export.csv",
            "/api/admin/dashboard/hot-items",
            "/api/admin/users/{user_id}/debug",
            "/api/admin/requests/{request_id}",
            "/api/admin/models",
            "/api/admin/models/compare",
            "/api/admin/training-jobs",
            "/api/admin/items",
            "/api/admin/operations",
        ]
        expected_read_roles = ["operator_readonly", "operator", "admin"]
        for path in read_paths:
            with self.subTest(path=path):
                operation = schema["paths"][path]["get"]
                self.assertEqual(operation["security"], [{"cookieAuth": []}])
                self.assertEqual(operation["x-required-roles"], expected_read_roles)

        for path in ["/api/admin/promotions", "/api/admin/operation-batches"]:
            operation = schema["paths"][path]["post"]
            self.assertEqual(operation["security"], [{"cookieAuth": [], "csrfHeader": []}])
            self.assertEqual(operation["x-required-roles"], ["operator", "admin"])

        item_detail = schema["paths"]["/api/items/{item_id}"]["get"]
        self.assertEqual(item_detail["security"], [{"cookieAuth": []}])
        self.assertEqual(
            item_detail["x-required-roles"],
            ["user", "operator_readonly", "operator", "admin"],
        )
        schemas = schema["components"]["schemas"]
        self.assertEqual(
            set(schemas["AdminItemResponse"]["properties"]),
            {
                "item_id",
                "title",
                "heat",
                "online_status",
                "updated_at",
                "state_version",
                "cover",
            },
        )
        self.assertIn("offline_item_count", schemas["DashboardOverview"]["properties"])
        self.assertIn("feed_share", schemas["DashboardFeedDiagnostics"]["properties"])
        for field in (
            "operator_id",
            "operator_role",
            "operation_type",
            "reason",
            "targets",
        ):
            self.assertIn(field, schemas["AuditOperationResponse"]["properties"])

    def test_internal_activation_has_publish_token_and_no_public_routes(self) -> None:
        app = FastAPI(docs_url=None, redoc_url=None)
        app.include_router(
            build_internal_activation_router(
                get_session=self.get_session,
                service=ActivationService(publish_token="p" * 32, loader=Loader()),
                runtime=Runtime(),
            )
        )
        schema = app.openapi()
        self.assertEqual(set(schema["paths"]), {"/internal/model-versions/{version}/activate"})
        operation = schema["paths"]["/internal/model-versions/{version}/activate"]["post"]
        self.assertEqual(operation["security"], [{"publishToken": []}])
        self.assertTrue(operation["x-internal-only"])

    def test_invalid_publish_token_precedes_version_lookup_and_leaves_no_audit_mutation(
        self,
    ) -> None:
        version = "token-order-v1"
        with self.factory.begin() as session:
            session.add(
                ModelVersion(
                    model_version=version,
                    data_version="data-v1",
                    config_checksum="a" * 64,
                    metrics={},
                    artifact_uri="bundle",
                    artifact_checksum="b" * 64,
                    manifest_checksum="c" * 64,
                    purpose=EvaluationPurpose.BASE_OFFICIAL,
                    evaluation_comparability=Comparability.COMPARABLE,
                    activation_eligible=True,
                    status=ModelStatus.READY,
                    failure_reason=None,
                    trained_at=NOW,
                )
            )
        app = FastAPI()
        install_api_error_handlers(app)
        app.include_router(
            build_internal_activation_router(
                get_session=self.get_session,
                service=ActivationService(publish_token="p" * 32, loader=Loader()),
                runtime=Runtime(),
            )
        )
        payload = {"expected_current_version": None, "manifest_checksum": "c" * 64}
        statements: list[str] = []

        def capture_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        sqlalchemy_event.listen(self.engine, "before_cursor_execute", capture_statement)
        try:
            with TestClient(app) as client:
                existing = client.post(
                    f"/internal/model-versions/{version}/activate",
                    json=payload,
                    headers={"X-Publish-Token": "wrong"},
                )
                missing = client.post(
                    "/internal/model-versions/nonexistent/activate",
                    json=payload,
                    headers={"X-Publish-Token": "wrong"},
                )
        finally:
            sqlalchemy_event.remove(self.engine, "before_cursor_execute", capture_statement)
        self.assertEqual((existing.status_code, missing.status_code), (401, 401))
        self.assertEqual(existing.json()["code"], "invalid_publish_token")
        self.assertEqual(missing.json()["code"], "invalid_publish_token")
        self.assertEqual(statements, [])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count(ModelActivationAttempt.id))), 0)
            self.assertIsNone(session.get(ModelVersion, version).failure_reason)


if __name__ == "__main__":
    unittest.main()
