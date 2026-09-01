from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.app.auth import (
    AuthService,
    CookieSettings,
    JWTService,
    JWTSettings,
    PasswordService,
    build_auth_dependencies,
    build_auth_router,
    install_api_error_handlers,
)
from apps.api.app.auth.errors import ApiError
from apps.api.app.auth.rate_limit import InMemoryRegistrationLimiter
from apps.api.app.db.models import (
    FeedType,
    Item,
    OnlineStatus,
    Operation,
    OperationBatch,
    OperationBatchStatus,
    Role,
)
from apps.api.app.db.session import session_dependency
from apps.api.app.operations.router import build_operations_router
from apps.api.app.operations.schemas import OperationBatchRequest
from apps.api.app.operations.service import AuditedOperationFailure, OperationService

from ._support import NOW, PASSWORD, add_user, factory_for, sqlite_engine


def request(
    operation_type: str,
    targets: list[str],
    *,
    starts_at=NOW,
    scope_type: str = "all",
    scope_value: str | None = None,
) -> OperationBatchRequest:
    return OperationBatchRequest.model_validate(
        {
            "batch_id": uuid.uuid4(),
            "operation_type": operation_type,
            "targets": targets,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "starts_at_utc": starts_at,
            "ends_at_utc": starts_at + timedelta(hours=2),
            "priority": 10 if operation_type == "promote" else 0,
            "target_position": 0 if operation_type == "promote" else None,
            "reason": "verified fixture operation",
            "semantics": "preflight_then_all_or_nothing_transaction",
        }
    )


class OperationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.service = OperationService()
        with self.factory.begin() as session:
            self.operator = add_user(session, username="ops-user", role=Role.OPERATOR)
            session.add_all(
                [
                    Item(id="item-1", title="One"),
                    Item(id="item-2", title="Two"),
                    Item(id="item-3", title="Three"),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_preflight_and_mid_batch_failure_are_all_or_nothing_with_audit(self) -> None:
        payload = request("offline", ["item-1", "item-2"])
        calls = 0

        def fail_second(_item_id: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected")

        with self.factory() as session:
            with self.assertRaises(AuditedOperationFailure):
                self.service.create_batch(
                    session,
                    operator_id=self.operator.id,
                    request=payload,
                    now=NOW,
                    before_each_apply=fail_second,
                )
            session.commit()
        with self.factory() as session:
            items = session.scalars(select(Item).order_by(Item.id)).all()
            self.assertTrue(all(item.online_status == OnlineStatus.ONLINE for item in items))
            batch = session.get(OperationBatch, payload.batch_id)
            assert batch is not None
            self.assertEqual(batch.status, OperationBatchStatus.FAILED)
            self.assertEqual(batch.operator_role, Role.OPERATOR)
            audits = session.scalars(
                select(Operation).where(Operation.batch_id == payload.batch_id)
            ).all()
            self.assertEqual(len(audits), 2)
            self.assertTrue(all(row.result == "failed" for row in audits))
        with self.factory() as session, self.assertRaises(AuditedOperationFailure) as replayed:
            self.service.create_batch(
                session,
                operator_id=self.operator.id,
                request=payload,
                now=NOW,
            )
        self.assertEqual(replayed.exception.code, "operation_transaction_failed")
        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(Operation.batch_id == payload.batch_id)
                ),
                2,
            )

        missing = request("offline", ["item-1", "missing"])
        with self.factory() as session:
            with self.assertRaises(AuditedOperationFailure):
                self.service.create_batch(
                    session, operator_id=self.operator.id, request=missing, now=NOW
                )
            session.commit()
        with self.factory() as session:
            self.assertEqual(session.get(Item, "item-1").online_status, OnlineStatus.ONLINE)

    def test_offline_restore_promotion_and_offline_authority(self) -> None:
        offline = request("offline", ["item-1", "item-2"])
        with self.factory() as session:
            result = self.service.create_batch(
                session, operator_id=self.operator.id, request=offline, now=NOW
            )
            session.commit()
        self.assertEqual(result.status, "succeeded")
        with self.factory() as session:
            self.assertEqual(session.get(Item, "item-1").online_status, OnlineStatus.OFFLINE)

        promotion = request("promote", ["item-1"], scope_type="feed", scope_value="popular")
        with self.factory() as session:
            self.service.create_batch(
                session, operator_id=self.operator.id, request=promotion, now=NOW
            )
            session.commit()
        with self.factory() as session:
            active = self.service.active_promotions(session, now=NOW, feed_type=FeedType.POPULAR)
            self.assertEqual(active, [], "promotion cannot revive an offline item")

        restore = request("restore", ["item-1"])
        with self.factory() as session:
            self.service.create_batch(
                session, operator_id=self.operator.id, request=restore, now=NOW
            )
            session.commit()
        with self.factory() as session:
            active = self.service.active_promotions(session, now=NOW, feed_type=FeedType.POPULAR)
            self.assertEqual([rule.item_id for rule in active], ["item-1"])

    def test_scheduled_batch_uses_injected_clock_and_state_cas(self) -> None:
        scheduled = request("offline", ["item-3"], starts_at=NOW + timedelta(hours=1))
        with self.factory() as session:
            response = self.service.create_batch(
                session, operator_id=self.operator.id, request=scheduled, now=NOW
            )
            session.commit()
        self.assertEqual(response.status, "scheduled")
        with self.factory() as session:
            self.assertEqual(
                self.service.apply_due_batches(session, now=NOW + timedelta(minutes=30)), []
            )
            session.commit()
        with self.factory() as session:
            applied = self.service.apply_due_batches(session, now=NOW + timedelta(hours=1))
            session.commit()
        self.assertEqual(applied[0].status, "succeeded")
        with self.factory() as session:
            self.assertEqual(session.get(Item, "item-3").online_status, OnlineStatus.OFFLINE)
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(
                        Operation.batch_id == scheduled.batch_id,
                        Operation.result == "succeeded",
                    )
                ),
                1,
            )

    def test_batch_replay_is_existing_and_payload_conflict_is_409(self) -> None:
        payload = request("offline", ["item-1"])
        with self.factory() as session:
            first = self.service.create_batch(
                session, operator_id=self.operator.id, request=payload, now=NOW
            )
            session.commit()
        with self.factory() as session:
            replay = self.service.create_batch(
                session, operator_id=self.operator.id, request=payload, now=NOW
            )
            session.commit()
        self.assertEqual(replay, first)
        with self.factory() as session:
            self.assertEqual(session.get(Item, "item-1").state_version, 1)
            self.assertEqual(
                session.scalar(
                    select(func.count(Operation.id)).where(Operation.batch_id == payload.batch_id)
                ),
                1,
            )
        conflict = payload.model_copy(update={"reason": "different audited reason"})
        with self.factory() as session, self.assertRaises(ApiError) as raised:
            self.service.create_batch(
                session, operator_id=self.operator.id, request=conflict, now=NOW
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_admin_item_search_status_filter_and_full_audit_response(self) -> None:
        with self.factory.begin() as session:
            item = session.get(Item, "item-2")
            item.online_status = OnlineStatus.OFFLINE
            item.likes_snapshot = 4
            item.views_snapshot = 6
        auth = AuthService(
            PasswordService(),
            JWTService(JWTSettings(secret="operations-api-test-secret-longer-than-32-bytes")),
        )
        get_session = session_dependency(self.factory)
        dependencies = build_auth_dependencies(get_session, auth)
        app = FastAPI()
        install_api_error_handlers(app)
        app.include_router(
            build_auth_router(
                get_session=get_session,
                service=auth,
                dependencies=dependencies,
                limiter=InMemoryRegistrationLimiter(limit=10),
                cookies=CookieSettings(secure=False),
            )
        )
        app.include_router(
            build_operations_router(
                get_session=get_session,
                dependencies=dependencies,
                service=self.service,
            )
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login", json={"username": "ops-user", "password": PASSWORD}
            )
            self.assertEqual(login.status_code, 200)
            response = client.get(
                "/api/admin/items", params={"query": "item-2", "online_status": "offline"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                [
                    {
                        "item_id": "item-2",
                        "title": "Two",
                        "heat": 10,
                        "online_status": "offline",
                        "updated_at": response.json()[0]["updated_at"],
                        "state_version": 0,
                        "cover": None,
                    }
                ],
            )

            payload = request("restore", ["item-2"])
            csrf = client.cookies.get("microlens_csrf")
            created = client.post(
                "/api/admin/operation-batches",
                json=payload.model_dump(mode="json"),
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(created.status_code, 201)
            audit = client.get("/api/admin/operations")
            self.assertEqual(audit.status_code, 200)
            row = audit.json()[0]
            self.assertEqual(row["operator_id"], str(self.operator.id))
            self.assertEqual(row["operator_role"], "operator")
            self.assertEqual(row["operation_type"], "restore")
            self.assertEqual(row["reason"], "verified fixture operation")
            self.assertEqual(row["targets"], ["item-2"])
            self.assertEqual(row["target"], "item-2")
            self.assertEqual(row["result"], "succeeded")

            failed = request("offline", ["missing-item"])
            first_failure = client.post(
                "/api/admin/operation-batches",
                json=failed.model_dump(mode="json"),
                headers={"X-CSRF-Token": csrf},
            )
            replay_failure = client.post(
                "/api/admin/operation-batches",
                json=failed.model_dump(mode="json"),
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual((first_failure.status_code, replay_failure.status_code), (422, 422))
            self.assertEqual(first_failure.json()["code"], replay_failure.json()["code"])
            audits = [
                row
                for row in client.get("/api/admin/operations").json()
                if row["batch_id"] == str(failed.batch_id)
            ]
            self.assertEqual(len(audits), 1)


if __name__ == "__main__":
    unittest.main()
