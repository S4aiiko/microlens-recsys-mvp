from __future__ import annotations

import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.db.models import (
    AccountStatus,
    Item,
    OnlineStatus,
    Role,
    User,
)
from apps.api.app.search.domain import SearchPermissionDenied, SearchPrincipal, SearchQuery
from apps.api.app.search.postgres import SqlAlchemyPostgresSearchAuthority
from tests.search._support import NOW


class SqlAlchemyAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        User.__table__.create(self.engine)
        Item.__table__.create(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.enabled_id = uuid.uuid4()
        self.disabled_id = uuid.uuid4()
        with self.factory.begin() as session:
            session.add_all(
                [
                    User(
                        id=self.enabled_id,
                        username="search-user",
                        username_normalized="search-user",
                        password_hash="not-a-real-password",
                        role=Role.USER,
                        status=AccountStatus.ENABLED,
                    ),
                    User(
                        id=self.disabled_id,
                        username="disabled-search-user",
                        username_normalized="disabled-search-user",
                        password_hash="not-a-real-password",
                        role=Role.USER,
                        status=AccountStatus.DISABLED,
                    ),
                    Item(
                        id="a",
                        title="Python 100% guide",
                        likes_snapshot=10,
                        views_snapshot=100,
                        online_status=OnlineStatus.ONLINE,
                        state_version=2,
                        updated_at=NOW,
                    ),
                    Item(
                        id="b",
                        title="Python offline",
                        likes_snapshot=20,
                        views_snapshot=200,
                        online_status=OnlineStatus.OFFLINE,
                        state_version=3,
                        updated_at=NOW,
                    ),
                    Item(
                        id="c",
                        title="Python basic",
                        likes_snapshot=5,
                        views_snapshot=50,
                        online_status=OnlineStatus.ONLINE,
                        state_version=1,
                        updated_at=NOW,
                    ),
                    Item(
                        id="d",
                        title="Unrelated",
                        likes_snapshot=99,
                        views_snapshot=999,
                        online_status=OnlineStatus.ONLINE,
                        state_version=0,
                        updated_at=NOW,
                    ),
                ]
            )
        self.authority = SqlAlchemyPostgresSearchAuthority(self.factory)
        self.principal = SearchPrincipal(self.enabled_id)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_current_pg_filters_online_and_returns_current_values(self) -> None:
        allowed, permission_filtered = self.authority.authorize_hits(
            SearchQuery("Python"), self.principal, ("a", "b", "c", "missing")
        )
        self.assertEqual(set(allowed), {"a", "c"})
        self.assertEqual(allowed["a"].state_version, 2)
        self.assertEqual(permission_filtered, 0)
        fallback = self.authority.fallback_search(
            SearchQuery("Python"), self.principal, exclude_item_ids=("a",), limit=10
        )
        self.assertEqual([result.item_id for result in fallback], ["c"])

    def test_sql_wildcards_are_literal_and_offline_items_remain_excluded(self) -> None:
        percent = self.authority.fallback_search(
            SearchQuery("%"), self.principal, exclude_item_ids=(), limit=10
        )
        self.assertEqual([result.item_id for result in percent], ["a"])
        current = self.authority.current_items(("a", "b", "missing"))
        self.assertTrue(current["a"].online)
        self.assertFalse(current["b"].online)

    def test_streaming_source_is_sorted_and_excludes_offline(self) -> None:
        batches = list(self.authority.iter_online_documents(batch_size=2))
        self.assertEqual(
            [[item.item_id for item in batch] for batch in batches],
            [["a", "c"], ["d"]],
        )

    def test_disabled_or_missing_principal_is_denied_from_pg(self) -> None:
        with self.assertRaises(SearchPermissionDenied):
            self.authority.fallback_search(
                SearchQuery("Python"),
                SearchPrincipal(self.disabled_id),
                exclude_item_ids=(),
                limit=10,
            )
        with self.assertRaises(SearchPermissionDenied):
            self.authority.authorize_hits(
                SearchQuery("Python"), SearchPrincipal(uuid.uuid4()), ("a",)
            )


if __name__ == "__main__":
    unittest.main()
