from __future__ import annotations

import unittest
import uuid
from math import nan

from apps.api.app.search.domain import (
    READ_ALIAS,
    AuthorityUnavailable,
    ProjectionHit,
    SearchPermissionDenied,
    SearchPrincipal,
    SearchQuery,
)
from apps.api.app.search.service import AuthoritativeSearchService
from tests.search._support import FakeAuthority, FakeProjection, item


class AuthoritativeSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projection = FakeProjection()
        self.projection.indices["microlens-items-v1"] = {
            "a": item("a", "Python stale title", likes=30),
            "b": item("b", "Python offline", likes=20),
            "c": item("c", "Python current", likes=10),
        }
        self.projection.aliases[READ_ALIAS] = ("microlens-items-v1",)
        self.authority = FakeAuthority(
            [
                item("a", "Python authoritative title", state_version=4, likes=30),
                item("b", "Python offline", online=False, state_version=8, likes=20),
                item("c", "Python current", likes=10),
                item("d", "Python backfill", likes=5),
            ]
        )
        self.principal = SearchPrincipal(uuid.uuid4())
        self.service = AuthoritativeSearchService(self.projection, self.authority)

    def test_every_hit_is_pg_verified_and_offline_is_backfilled(self) -> None:
        response = self.service.search(SearchQuery("Python", limit=3), self.principal)
        self.assertEqual([result.item.item_id for result in response.items], ["a", "c", "d"])
        self.assertEqual(response.items[0].item.title, "Python authoritative title")
        self.assertEqual(response.items[0].item.state_version, 4)
        self.assertNotIn("b", [result.item.item_id for result in response.items])
        self.assertEqual(response.stale_hits_filtered, 1)
        self.assertTrue(response.degraded)

    def test_current_title_no_longer_matching_query_is_not_returned(self) -> None:
        self.authority.items["a"] = item("a", "Rust authoritative title", state_version=5)
        response = self.service.search(SearchQuery("Python", limit=4), self.principal)
        self.assertNotIn("a", [result.item.item_id for result in response.items])

    def test_current_permissions_filter_projection_hit(self) -> None:
        self.authority.denied_item_ids.add("a")
        response = self.service.search(SearchQuery("Python", limit=3), self.principal)
        self.assertNotIn("a", [result.item.item_id for result in response.items])
        self.assertEqual(response.permission_hits_filtered, 1)

    def test_projection_failure_uses_only_postgresql_fallback(self) -> None:
        self.projection.reachable = False
        response = self.service.search(SearchQuery("Python", limit=2), self.principal)
        self.assertEqual(response.source, "postgresql_fallback")
        self.assertEqual([result.item.item_id for result in response.items], ["a", "c"])
        self.assertTrue(response.degraded)

    def test_postgresql_failure_never_leaks_unverified_projection_hits(self) -> None:
        self.authority.fail = True
        with self.assertRaises(AuthorityUnavailable):
            self.service.search(SearchQuery("Python", limit=2), self.principal)

    def test_disabled_principal_is_not_hidden_as_service_fallback(self) -> None:
        self.authority.denied_principals.add(self.principal.user_id)
        with self.assertRaises(SearchPermissionDenied):
            self.service.search(SearchQuery("Python"), self.principal)

    def test_non_finite_projection_score_is_rejected_at_the_protocol_boundary(self) -> None:
        with self.assertRaises(ValueError):
            ProjectionHit("a", nan, 0, "microlens-items-v1")
        with self.assertRaises(ValueError):
            SearchQuery("Python", limit=True)


if __name__ == "__main__":
    unittest.main()
