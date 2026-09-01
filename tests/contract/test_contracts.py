from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

try:
    from jsonschema.validators import validator_for
except ModuleNotFoundError:  # The Docker/dev extra provides this; base host may not.
    validator_for = None

try:
    import yaml
except ModuleNotFoundError:  # Complete contract tests require the declared dev extra.
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"


class ContractFilesTest(unittest.TestCase):
    def test_json_files_parse(self) -> None:
        for contract_file in sorted(CONTRACTS.glob("*.json")):
            with self.subTest(contract_file=contract_file.name):
                json.loads(contract_file.read_text(encoding="utf-8"))

    def test_json_schemas_are_valid(self) -> None:
        if validator_for is None:
            self.skipTest("jsonschema is not installed in the base host interpreter")
        for schema_file in sorted(CONTRACTS.glob("*.schema.json")):
            with self.subTest(schema_file=schema_file.name):
                schema = json.loads(schema_file.read_text(encoding="utf-8"))
                validator_for(schema).check_schema(schema)

    def test_contract_index_targets_exist(self) -> None:
        index = json.loads((CONTRACTS / "contract-index.json").read_text(encoding="utf-8"))
        for target in index["files"].values():
            self.assertTrue((CONTRACTS / target).is_file(), target)

    def test_database_er_contains_enforcement_and_audit_fields(self) -> None:
        database = json.loads((CONTRACTS / "db-er.json").read_text(encoding="utf-8"))
        tables = database["tables"]
        self.assertIn("payload_hash", tables["event_batches"]["fields"])
        self.assertTrue(
            {
                "purpose",
                "evaluation_comparability",
                "activation_eligible",
            }.issubset(tables["model_versions"]["fields"])
        )
        self.assertTrue(
            {
                "expected_state_version",
                "status",
                "scheduled_at",
                "started_at",
                "completed_at",
            }.issubset(tables["operation_batches"]["fields"])
        )

    def test_openapi_route_and_security_coverage(self) -> None:
        document = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(document["openapi"], "3.1.0")
        required_paths = {
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/me",
            "/api/auth/logout",
            "/api/feeds/{feed_type}",
            "/api/events",
            "/api/events/batch",
            "/api/profile/me",
            "/api/admin/dashboard/overview",
            "/api/admin/dashboard/timeseries",
            "/api/admin/dashboard/feeds",
            "/api/admin/dashboard/export.csv",
            "/api/admin/users/{user_id}/debug",
            "/api/admin/requests/{request_id}",
            "/api/admin/models",
            "/api/admin/models/compare",
            "/api/admin/training-jobs",
            "/api/admin/items",
            "/api/admin/promotions",
            "/api/admin/operations",
            "/api/admin/operation-batches",
            "/api/admin/users",
            "/api/admin/roles",
            "/api/items/{item_id}",
            "/health",
            "/ready",
        }
        self.assertTrue(required_paths.issubset(document["paths"]))
        self.assertEqual(
            document["components"]["schemas"]["Role"]["enum"],
            ["user", "operator_readonly", "operator", "admin"],
        )
        self.assertEqual(
            document["components"]["schemas"]["ServerEventType"]["enum"],
            ["impression", "click", "like", "not_interested", "dwell", "revisit", "share"],
        )
        self.assertEqual(
            document["components"]["schemas"]["ClientEventType"]["enum"],
            ["click", "like", "not_interested", "dwell", "revisit", "share"],
        )

    def test_openapi_protected_routes_have_security_roles_and_typed_responses(self) -> None:
        document = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
        readonly_roles = ["operator_readonly", "operator", "admin"]
        all_roles = ["user", *readonly_roles]
        role_matrix = {
            ("/api/auth/me", "get"): all_roles,
            ("/api/auth/logout", "post"): all_roles,
            ("/api/feeds/{feed_type}", "get"): all_roles,
            ("/api/events", "post"): all_roles,
            ("/api/events/batch", "post"): all_roles,
            ("/api/profile/me", "get"): all_roles,
            ("/api/items/{item_id}", "get"): all_roles,
            ("/api/admin/dashboard/overview", "get"): readonly_roles,
            ("/api/admin/dashboard/timeseries", "get"): readonly_roles,
            ("/api/admin/dashboard/feeds", "get"): readonly_roles,
            ("/api/admin/dashboard/export.csv", "get"): readonly_roles,
            ("/api/admin/users/{user_id}/debug", "get"): readonly_roles,
            ("/api/admin/requests/{request_id}", "get"): readonly_roles,
            ("/api/admin/models", "get"): readonly_roles,
            ("/api/admin/models/compare", "get"): readonly_roles,
            ("/api/admin/training-jobs", "get"): readonly_roles,
            ("/api/admin/items", "get"): readonly_roles,
            ("/api/admin/promotions", "post"): ["operator", "admin"],
            ("/api/admin/operations", "get"): readonly_roles,
            ("/api/admin/operation-batches", "post"): ["operator", "admin"],
            ("/api/admin/users", "get"): ["admin"],
            ("/api/admin/roles", "put"): ["admin"],
        }

        operation_ids: set[str] = set()
        for (path, method), expected_roles in role_matrix.items():
            with self.subTest(path=path, method=method):
                path_item = document["paths"][path]
                if "$ref" in path_item:
                    component_name = path_item["$ref"].rsplit("/", 1)[-1]
                    path_item = document["components"]["pathItems"][component_name]
                operation = path_item[method]
                security = operation["security"]
                self.assertTrue(any("cookieAuth" in requirement for requirement in security))
                if method in {"post", "put", "patch", "delete"}:
                    self.assertTrue(any("csrfHeader" in requirement for requirement in security))
                self.assertEqual(operation["x-required-roles"], expected_roles)
                operation_id = operation["operationId"]
                self.assertNotIn(operation_id, operation_ids)
                operation_ids.add(operation_id)
                success_responses = [
                    response
                    for status, response in operation["responses"].items()
                    if status.startswith("2")
                ]
                self.assertTrue(success_responses)
                if path.startswith("/api/admin/"):
                    self.assertTrue(
                        all("content" in response for response in success_responses),
                        f"admin response must be typed: {operation_id}",
                    )

        listeners = document["x-listener-contract"]
        self.assertEqual(listeners["public"]["reject_path_prefixes"], ["/internal/"])
        self.assertFalse(any(path.startswith("/internal/") for path in document["paths"]))
        public_text = json.dumps(document, sort_keys=True)
        for forbidden in (
            "publishToken",
            "X-Publish-Token",
            "activateModelVersion",
            "ActivationRequest",
        ):
            self.assertNotIn(forbidden, public_text)

    def test_internal_openapi_is_self_contained_and_internal_only(self) -> None:
        document = json.loads(
            (CONTRACTS / "internal-openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertEqual(document["servers"][0]["url"], "http://api:8001")
        self.assertEqual(
            set(document["paths"]), {"/internal/model-versions/{version}/activate"}
        )
        operation = document["paths"]["/internal/model-versions/{version}/activate"]["post"]
        self.assertEqual(operation["operationId"], "activateModelVersion")
        self.assertEqual(operation["security"], [{"publishToken": []}])
        scheme = document["components"]["securitySchemes"]["publishToken"]
        self.assertEqual(scheme, {"type": "apiKey", "in": "header", "name": "X-Publish-Token"})
        self.assertFalse(document["x-listener-contract"]["host_published"])
        self.assertEqual(
            document["x-listener-contract"]["network_scope"], "compose-internal-only"
        )
        self.assertEqual(
            set(document["components"]["schemas"]),
            {"ActivationRequest", "ModelVersion", "ErrorEnvelope"},
        )

        def walk(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str):
                    self.assertTrue(reference.startswith("#/"), reference)
                    resolved: object = document
                    for segment in reference[2:].split("/"):
                        self.assertIsInstance(resolved, dict)
                        resolved = resolved[segment]  # type: ignore[index]
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(document)

    def test_client_event_contract_rejects_server_impression(self) -> None:
        if validator_for is None:
            self.skipTest("jsonschema is not installed; install the declared dev extra")
        document = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
        root_validator = validator_for(document)(document)
        event_validator = root_validator.evolve(
            schema=document["components"]["schemas"]["EventRequest"]
        )
        impression = {
            "event_id": "00000000-0000-4000-8000-000000000001",
            "request_id": "00000000-0000-4000-8000-000000000002",
            "item_id": "item-1",
            "position": 0,
            "event_type": "impression",
            "client_timestamp": "2026-09-01T00:00:00Z",
        }
        self.assertTrue(list(event_validator.iter_errors(impression)))
        batch_validator = root_validator.evolve(
            schema=document["components"]["schemas"]["EventBatchRequest"]
        )
        self.assertTrue(
            list(
                batch_validator.iter_errors(
                    {
                        "batch_id": "00000000-0000-4000-8000-000000000003",
                        "events": [impression],
                    }
                )
            )
        )
        persisted_validator = root_validator.evolve(
            schema=document["components"]["schemas"]["PersistedEvent"]
        )
        persisted = {**impression, "server_timestamp": "2026-09-01T00:00:01Z"}
        self.assertFalse(list(persisted_validator.iter_errors(persisted)))

    def test_generated_admin_sdk_preserves_security_and_response_types(self) -> None:
        sdk = (ROOT / "apps/web/src/api/generated/sdk.gen.ts").read_text(encoding="utf-8")
        types = (ROOT / "apps/web/src/api/generated/types.gen.ts").read_text(
            encoding="utf-8"
        )
        operations = [
            "getDashboardOverview", "getDashboardTimeseries", "getDashboardFeeds",
            "exportDashboardCsv", "debugUser", "debugRecommendationRequest",
            "listModelVersions", "compareModelVersions", "listTrainingJobs",
            "searchAdminItems", "createPromotion", "listOperations",
            "createOperationBatch", "listAdminUsers", "updateUserRole",
        ]
        for operation_id in operations:
            with self.subTest(operation_id=operation_id):
                match = re.search(
                    rf"export const {operation_id} = .*?\n\}}\);",
                    sdk,
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(match, operation_id)
                self.assertIn("security:", match.group(0))
                type_name = operation_id[0].upper() + operation_id[1:] + "Responses"
                response = re.search(
                    rf"export type {type_name} = \{{(.*?)\n\}};",
                    types,
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(response, type_name)
                self.assertNotIn(": unknown;", response.group(1))

        generated_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "apps/web/src/api/generated").rglob("*.ts"))
        )
        for forbidden in (
            "activateModelVersion",
            "ActivateModelVersion",
            "X-Publish-Token",
            "publishToken",
            "/internal/",
        ):
            self.assertNotIn(forbidden, generated_text)

    def test_compose_has_exact_foundation_services_and_internal_network(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML is not installed; complete tests require project[dev]")
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(compose["services"]), {"web", "api", "db", "redis", "worker"})
        self.assertTrue(compose["networks"]["backend"]["internal"])
        required_volumes = {"postgres_data", "redis_data", "model_artifacts", "training_exports"}
        self.assertTrue(required_volumes.issubset(compose["volumes"]))
        self.assertIn("model_artifacts", compose["services"]["api"]["volumes"][0])
        self.assertIn("model_artifacts", compose["services"]["worker"]["volumes"][0])
        listeners = compose["x-listener-contract"]
        self.assertEqual(listeners["internal_activation"]["url"], "http://api:8001")
        self.assertFalse(listeners["internal_activation"]["host_published"])
        self.assertEqual(listeners["public"]["reject_path_prefixes"], ["/internal/"])
        self.assertEqual(compose["services"]["api"]["ports"], ["${API_PORT:-8000}:8000"])
        self.assertIn("8001", compose["services"]["api"]["expose"])

    def test_stable_make_targets_are_present(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        targets = {
            "doctor", "data-inspect", "data-download", "smoke-all", "up", "up-core", "ps",
            "logs", "test", "down", "full-data", "train-full", "export-events",
            "build-training-data", "train-async", "worker-run-once", "job-status",
            "cache-stats", "publish", "prepare-7b-fixture", "covers",
        }
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(f"{target}:", makefile)

    def test_sensitive_and_large_paths_are_ignored(self) -> None:
        ignored = [
            ".env",
            ".DS_Store",
            "dataset/MicroLens-50k_pairs.csv",
            "MicroLens-master/README.md",
            "data/processed/example/train.parquet",
            "artifacts/models/example/checkpoint.pt",
            "logs/api.log",
            "cache/key.bin",
            "tmp/file",
            ".assignment-flow/assignment_state.md",
        ]
        for candidate in ignored:
            with self.subTest(candidate=candidate):
                result = subprocess.run(
                    ["git", "check-ignore", "-q", candidate], cwd=ROOT, check=False
                )
                self.assertEqual(result.returncode, 0, candidate)
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"], cwd=ROOT, check=False
        )
        self.assertNotEqual(result.returncode, 0)

    def test_docker_context_excludes_private_and_generated_trees(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = {
            line.strip()
            for line in dockerignore
            if line.strip() and not line.lstrip().startswith("#")
        }
        required_patterns = {
            ".git/",
            ".assignment-flow/",
            ".env",
            ".env.*",
            "secrets/",
            "dataset/",
            "MicroLens*/",
            "data/",
            "artifacts/",
            "models/",
            "checkpoints/",
            "training_exports/",
            "**/node_modules/",
            "apps/web/dist/",
        }
        self.assertTrue(required_patterns.issubset(patterns))


if __name__ == "__main__":
    unittest.main()
