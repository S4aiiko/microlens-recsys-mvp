from __future__ import annotations

import unittest
from math import inf
from types import SimpleNamespace

from apps.api.app.search.domain import (
    READ_ALIAS,
    IndexBuildConflict,
    ProjectionUnavailable,
    SearchQuery,
)
from apps.api.app.search.elasticsearch_adapter import ElasticsearchSearchProjection
from apps.api.app.search.indexing import INDEX_MAPPINGS, INDEX_SETTINGS
from tests.search._support import item


class Response:
    def __init__(self, body: object) -> None:
        self.body = body


class TransportFailure(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.meta = SimpleNamespace(status=status)


class FakeIndicesClient:
    def __init__(self, parent: FakeOfficialClient) -> None:
        self.parent = parent
        self.create_calls: list[dict[str, object]] = []
        self.refresh_calls: list[dict[str, object]] = []
        self.alias_calls: list[dict[str, object]] = []
        self.failure: Exception | None = None

    def create(self, **kwargs: object) -> Response:
        self._fail()
        self.create_calls.append(kwargs)
        self.parent.indices_data[str(kwargs["index"])] = {}
        return Response({"acknowledged": True})

    def exists(self, **kwargs: object) -> bool:
        self._fail()
        return str(kwargs["index"]) in self.parent.indices_data

    def get_alias(self, **kwargs: object) -> Response:
        self._fail()
        alias = str(kwargs["name"])
        targets = self.parent.aliases.get(alias)
        if targets is None:
            raise TransportFailure("not found", status=404)
        return Response({target: {"aliases": {alias: {}}} for target in targets})

    def refresh(self, **kwargs: object) -> Response:
        self._fail()
        self.refresh_calls.append(kwargs)
        return Response({"_shards": {"failed": 0}})

    def update_aliases(self, **kwargs: object) -> Response:
        self._fail()
        self.alias_calls.append(kwargs)
        for action in kwargs["actions"]:  # type: ignore[union-attr]
            if "remove" in action:
                value = action["remove"]
                alias = value["alias"]
                self.parent.aliases[alias] = tuple(
                    index for index in self.parent.aliases.get(alias, ()) if index != value["index"]
                )
            elif "add" in action:
                value = action["add"]
                self.parent.aliases[value["alias"]] = (value["index"],)
        return Response({"acknowledged": True})

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure


class FakeOfficialClient:
    def __init__(self) -> None:
        self.indices_data: dict[str, dict[str, dict[str, object]]] = {}
        self.aliases: dict[str, tuple[str, ...]] = {}
        self.indices = FakeIndicesClient(self)
        self.bulk_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.failure: Exception | None = None
        self.partial_failure_ids: set[str] = set()
        self.missing_delete_ids: set[str] = set()
        self.search_hits: list[dict[str, object]] = []
        self.timeout_values: list[float] = []

    def options(self, **kwargs: object) -> FakeOfficialClient:
        self.timeout_values.append(float(kwargs["request_timeout"]))
        return self

    def ping(self, **kwargs: object) -> bool:
        del kwargs
        self._fail()
        return True

    def bulk(self, **kwargs: object) -> Response:
        self._fail()
        self.bulk_calls.append(kwargs)
        operations = kwargs["operations"]
        cursor = 0
        items = []
        while cursor < len(operations):  # type: ignore[arg-type]
            metadata = operations[cursor]  # type: ignore[index]
            action_name, action = next(iter(metadata.items()))
            cursor += 2 if action_name == "index" else 1
            failed = action["_id"] in self.partial_failure_ids
            missing_delete = action_name == "delete" and action["_id"] in self.missing_delete_ids
            result = {
                "_index": action["_index"],
                "_id": action["_id"],
                "status": (
                    429
                    if failed
                    else (404 if missing_delete else (201 if action_name == "index" else 200))
                ),
            }
            if failed:
                result["error"] = {"type": "rejected"}
            if missing_delete:
                result["result"] = "not_found"
            items.append({action_name: result})
        return Response({"errors": bool(self.partial_failure_ids), "items": items})

    def count(self, **kwargs: object) -> Response:
        self._fail()
        return Response({"count": len(self.indices_data[str(kwargs["index"])])})

    def search(self, **kwargs: object) -> Response:
        self._fail()
        self.search_calls.append(kwargs)
        return Response({"hits": {"hits": self.search_hits}})

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure


class ElasticsearchAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeOfficialClient()
        self.adapter = ElasticsearchSearchProjection(self.client)
        self.index = "microlens-items-v1"

    def test_create_exists_count_and_seal_use_bounded_official_api_calls(self) -> None:
        self.assertFalse(self.adapter.index_exists(self.index))
        self.adapter.create_index(
            self.index,
            settings=INDEX_SETTINGS,
            mappings=INDEX_MAPPINGS,
        )
        self.assertTrue(self.adapter.index_exists(self.index))
        self.client.indices_data[self.index]["a"] = {}
        self.assertEqual(self.adapter.seal(self.index), 1)
        create = self.client.indices.create_calls[0]
        self.assertEqual(create["settings"], INDEX_SETTINGS)
        self.assertEqual(create["mappings"], INDEX_MAPPINGS)
        self.assertEqual(self.client.indices.refresh_calls[0]["index"], self.index)
        self.assertEqual(self.client.timeout_values, [5.0, 10.0, 5.0, 10.0, 10.0])

    def test_alias_lookup_missing_and_atomic_verified_switch(self) -> None:
        self.client.indices_data[self.index] = {}
        self.assertEqual(self.adapter.alias_targets(READ_ALIAS), ())
        self.adapter.switch_alias(READ_ALIAS, new_index=self.index, expected_old_index=None)
        self.assertEqual(self.adapter.alias_targets(READ_ALIAS), (self.index,))
        actions = self.client.indices.alias_calls[0]["actions"]
        self.assertEqual(actions, [{"add": {"index": self.index, "alias": READ_ALIAS}}])

        self.client.indices_data["microlens-items-v2"] = {}
        self.adapter.switch_alias(
            READ_ALIAS,
            new_index="microlens-items-v2",
            expected_old_index=self.index,
        )
        self.assertEqual(
            self.client.indices.alias_calls[1]["actions"],
            [
                {"remove": {"index": self.index, "alias": READ_ALIAS}},
                {"add": {"index": "microlens-items-v2", "alias": READ_ALIAS}},
            ],
        )
        with self.assertRaises(IndexBuildConflict):
            self.adapter.switch_alias(
                READ_ALIAS,
                new_index=self.index,
                expected_old_index="microlens-items-v9",
            )

    def test_bulk_contract_reports_partial_failures_without_response_error_leak(self) -> None:
        self.client.indices_data[self.index] = {}
        self.client.partial_failure_ids.add("b")
        result = self.adapter.bulk_apply(
            self.index,
            upserts=(item("a", "Alpha"), item("b", "Beta")),
            deletes=("c",),
        )
        self.assertEqual(result.succeeded, 2)
        self.assertEqual(result.failed_item_ids, ("b",))
        call = self.client.bulk_calls[0]
        self.assertFalse(call["refresh"])
        self.assertEqual(self.client.timeout_values[-1], 30.0)

    def test_bulk_rejects_overlapping_or_cross_index_response(self) -> None:
        self.client.indices_data[self.index] = {}
        with self.assertRaises(ValueError):
            self.adapter.bulk_apply(
                self.index,
                upserts=(item("a", "Alpha"),),
                deletes=("a",),
            )

        original = self.client.bulk

        def wrong_index(**kwargs: object) -> Response:
            response = original(**kwargs)
            response.body["items"][0]["index"]["_index"] = "other-project-v1"
            return response

        self.client.bulk = wrong_index  # type: ignore[method-assign]
        with self.assertRaisesRegex(ProjectionUnavailable, "target boundary"):
            self.adapter.bulk_apply(
                self.index,
                upserts=(item("a", "Alpha"),),
                deletes=(),
            )

    def test_missing_delete_is_an_idempotent_success(self) -> None:
        self.client.indices_data[self.index] = {}
        self.client.missing_delete_ids.add("already-gone")
        result = self.adapter.bulk_apply(
            self.index,
            upserts=(),
            deletes=("already-gone",),
        )
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed_item_ids, ())

    def test_search_requires_single_project_index_and_strict_hits(self) -> None:
        self.client.indices_data[self.index] = {}
        self.client.aliases[READ_ALIAS] = (self.index,)
        self.client.search_hits = [
            {
                "_index": self.index,
                "_id": "a",
                "_score": 2.5,
                "_source": {"item_id": "a", "state_version": 7},
            }
        ]
        hits = self.adapter.search(READ_ALIAS, SearchQuery("Alpha"), candidate_limit=25)
        self.assertEqual(hits[0].item_id, "a")
        self.assertEqual(hits[0].indexed_state_version, 7)
        call = self.client.search_calls[0]
        self.assertEqual(call["index"], READ_ALIAS)
        self.assertEqual(call["size"], 25)
        self.assertEqual(call["source_includes"], ["item_id", "state_version"])

        self.client.search_hits[0]["_score"] = inf
        with self.assertRaisesRegex(ProjectionUnavailable, "non-finite"):
            self.adapter.search(READ_ALIAS, SearchQuery("Alpha"), candidate_limit=25)

    def test_namespace_and_transport_failures_fail_closed_without_credentials(self) -> None:
        self.client.aliases[READ_ALIAS] = ("foreign-items-v1",)
        with self.assertRaisesRegex(ProjectionUnavailable, "outside"):
            self.adapter.alias_targets(READ_ALIAS)

        credential_marker = "credential-value"
        secret = "https://" + "test-user:" + credential_marker + "@invalid.example"
        self.client.failure = TransportFailure(secret, status=500)
        with self.assertRaises(ProjectionUnavailable) as raised:
            self.adapter.count(self.index)
        self.assertNotIn(credential_marker, str(raised.exception))
        self.assertNotIn("invalid.example", str(raised.exception))

    def test_embedded_credentials_are_rejected_before_client_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be embedded"):
            ElasticsearchSearchProjection.from_url(
                "http://" + "test-user:" + "credential-value" + "@localhost:9200"
            )
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            ElasticsearchSearchProjection.from_url(
                "http://localhost:9200", request_timeout_seconds=True
            )
        with self.assertRaisesRegex(ValueError, "query or fragment"):
            ElasticsearchSearchProjection.from_url("http://localhost:9200?token=secret")


if __name__ == "__main__":
    unittest.main()
