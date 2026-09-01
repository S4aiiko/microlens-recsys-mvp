from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api.app.auth import (
    AuthService,
    CookieSettings,
    JWTService,
    JWTSettings,
    PasswordService,
    build_auth_dependencies,
    build_auth_router,
    build_role_admin_router,
    install_api_error_handlers,
)
from apps.api.app.auth.rate_limit import (
    InMemoryRegistrationLimiter,
    RedisRegistrationLimiter,
    RegistrationLimiterUnavailable,
)
from apps.api.app.auth.schemas import RegisterRequest
from apps.api.app.auth.security import csrf_matches, normalize_username
from apps.api.app.db.models import AccountStatus, AuthSession, Role, User, UserProfile
from apps.api.app.db.seed import SEED_NAMESPACE, SEED_USERS, seed_demo_users
from apps.api.app.db.session import session_dependency

from ._support import NOW, PASSWORD, factory_for, sqlite_engine


class BrokenRedis:
    async def eval(
        self,
        _script: str,
        _numkeys: int,
        *_keys_and_args: str | int,
    ) -> object:
        raise ConnectionError("redis unavailable")


class CountingRedis:
    def __init__(self, *, value: int | None = None, has_ttl: bool = False) -> None:
        self.value = value
        self.has_ttl = has_ttl
        self.calls: list[tuple[str, int, tuple[str | int, ...]]] = []

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> object:
        self.calls.append((script, numkeys, keys_and_args))
        if self.value is None:
            self.value = 1
            self.has_ttl = True
            return self.value
        if not self.has_ttl:
            self.has_ttl = True
        self.value += 1
        return self.value


class ScriptErrorRedis:
    async def eval(
        self,
        _script: str,
        _numkeys: int,
        *_keys_and_args: str | int,
    ) -> object:
        raise RuntimeError("script execution failed")


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = sqlite_engine()
        self.factory = factory_for(self.engine)
        self.passwords = PasswordService()
        self.tokens = JWTService(JWTSettings(secret="a" * 48, lifetime=timedelta(hours=1)))
        self.service = AuthService(self.passwords, self.tokens)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_seed_is_idempotent_hashed_and_has_exact_role_counts(self) -> None:
        with self.factory.begin() as session:
            first = seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=self.passwords.hash,
                normalize_username=normalize_username,
            )
        with self.factory.begin() as session:
            second = seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=self.passwords.hash,
                normalize_username=normalize_username,
            )
        self.assertEqual([user.id for user in first], [user.id for user in second])
        with self.factory() as session:
            self.assertEqual(session.scalar(select(func.count(User.id))), len(SEED_USERS))
            roles = list(session.scalars(select(User.role).order_by(User.username)))
            self.assertEqual(roles.count(Role.USER), 3)
            self.assertEqual(roles.count(Role.OPERATOR_READONLY), 1)
            self.assertEqual(roles.count(Role.OPERATOR), 1)
            self.assertEqual(roles.count(Role.ADMIN), 1)
            for user in session.scalars(select(User)):
                self.assertNotEqual(user.password_hash, PASSWORD)
                self.assertTrue(user.password_hash.startswith("$argon2"))

    def test_seed_repairs_only_missing_profile_and_rejects_identity_drift(self) -> None:
        with self.factory.begin() as session:
            seeded = seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=self.passwords.hash,
                normalize_username=normalize_username,
            )
            repaired_user_id = seeded[0].id
            session.delete(session.get(UserProfile, repaired_user_id))
        with self.factory.begin() as session:
            seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=self.passwords.hash,
                normalize_username=normalize_username,
            )
            self.assertIsNotNone(session.get(UserProfile, repaired_user_id))

        drift_cases = {"role": Role.ADMIN, "status": AccountStatus.DISABLED}
        for field, value in drift_cases.items():
            with self.subTest(field=field):
                engine = sqlite_engine()
                factory = factory_for(engine)
                try:
                    with factory.begin() as session:
                        seed_demo_users(
                            session,
                            password=PASSWORD,
                            hash_password=self.passwords.hash,
                            normalize_username=normalize_username,
                        )
                    with self.assertRaisesRegex(ValueError, field):
                        with factory.begin() as session:
                            user = session.scalar(
                                select(User).where(User.username_normalized == "demo_user_a")
                            )
                            setattr(user, field, value)
                            session.flush()
                            seed_demo_users(
                                session,
                                password=PASSWORD,
                                hash_password=self.passwords.hash,
                                normalize_username=normalize_username,
                            )
                finally:
                    engine.dispose()

        engine = sqlite_engine()
        factory = factory_for(engine)
        conflicting_id = uuid.uuid4()
        try:
            with factory.begin() as session:
                session.add(
                    User(
                        id=conflicting_id,
                        username="demo_user_a",
                        username_normalized="demo_user_a",
                        password_hash=self.passwords.hash(PASSWORD),
                        role=Role.USER,
                        status=AccountStatus.ENABLED,
                    )
                )
            expected_id = uuid.uuid5(SEED_NAMESPACE, "demo_user_a")
            with self.assertRaisesRegex(ValueError, "id"):
                with factory.begin() as session:
                    seed_demo_users(
                        session,
                        password=PASSWORD,
                        hash_password=self.passwords.hash,
                        normalize_username=normalize_username,
                    )
            self.assertNotEqual(conflicting_id, expected_id)
        finally:
            engine.dispose()

    def test_register_login_revoke_expire_and_username_normalization(self) -> None:
        with self.factory.begin() as session:
            user = self.service.register(session, "  Alice  ", PASSWORD)
        self.assertEqual(user.role, Role.USER)
        self.assertEqual(user.username_normalized, "alice")
        with self.factory.begin() as session:
            with self.assertRaisesRegex(Exception, "already registered"):
                self.service.register(session, "ALICE", PASSWORD)
        with self.factory.begin() as session:
            user, issued = self.service.login(session, "alice", PASSWORD, now=NOW)
        self.assertTrue(csrf_matches(issued.csrf_token, issued.csrf_token, issued.csrf_digest))
        self.assertFalse(csrf_matches(issued.csrf_token, "forged", issued.csrf_digest))
        with self.factory() as session:
            authenticated = self.service.authenticate(session, issued.token, now=NOW)
            self.assertEqual(authenticated.user.id, user.id)
            self.service.revoke(session, authenticated, now=NOW)
            session.commit()
        with self.factory() as session:
            with self.assertRaisesRegex(Exception, "invalid or expired"):
                self.service.authenticate(session, issued.token, now=NOW)
        with self.factory.begin() as session:
            _user, expiring = self.service.login(session, "alice", PASSWORD, now=NOW)
        with self.factory() as session:
            with self.assertRaisesRegex(Exception, "invalid or expired"):
                self.service.authenticate(session, expiring.token, now=NOW + timedelta(hours=2))

    def test_weak_password_role_injection_and_atomic_redis_limiter(self) -> None:
        with self.assertRaises(ValidationError):
            RegisterRequest.model_validate(
                {"username": "mallory", "password": PASSWORD, "role": "admin"}
            )
        with self.factory.begin() as session:
            with self.assertRaisesRegex(Exception, "12"):
                self.service.register(session, "weak-user", "too-short")
        redis = CountingRedis()
        limiter = RedisRegistrationLimiter(redis, limit=1, window_seconds=30)
        self.assertTrue(asyncio.run(limiter.allow("127.0.0.1")))
        self.assertFalse(asyncio.run(limiter.allow("127.0.0.1")))
        self.assertEqual(len(redis.calls), 2)
        self.assertTrue(redis.has_ttl)
        _script, numkeys, keys_and_args = redis.calls[0]
        self.assertEqual(numkeys, 1)
        self.assertEqual(keys_and_args[-1], 30)

        dirty = CountingRedis(value=99, has_ttl=False)
        self.assertFalse(
            asyncio.run(RedisRegistrationLimiter(dirty, limit=5, window_seconds=30).allow("legacy"))
        )
        self.assertTrue(dirty.has_ttl)

        with self.assertRaises(RegistrationLimiterUnavailable):
            asyncio.run(RedisRegistrationLimiter(BrokenRedis()).allow("127.0.0.1"))
        with self.assertRaises(RegistrationLimiterUnavailable):
            asyncio.run(RedisRegistrationLimiter(ScriptErrorRedis()).allow("127.0.0.1"))

        with self.assertRaisesRegex(ValueError, "limit"):
            RedisRegistrationLimiter(redis, limit=0)
        with self.assertRaisesRegex(ValueError, "window"):
            RedisRegistrationLimiter(redis, window_seconds=0)

    def test_api_error_envelope_rbac_csrf_cookie_and_logout_revocation(self) -> None:
        with self.factory.begin() as session:
            seed_demo_users(
                session,
                password=PASSWORD,
                hash_password=self.passwords.hash,
                normalize_username=normalize_username,
            )
        get_session = session_dependency(self.factory)
        dependencies = build_auth_dependencies(get_session, self.service)
        app = FastAPI()
        install_api_error_handlers(app)
        app.include_router(
            build_auth_router(
                get_session=get_session,
                service=self.service,
                dependencies=dependencies,
                limiter=InMemoryRegistrationLimiter(limit=2),
                cookies=CookieSettings(secure=False, same_site="lax"),
            )
        )
        app.include_router(
            build_role_admin_router(get_session=get_session, dependencies=dependencies)
        )
        with TestClient(app) as client:
            anonymous = client.get("/api/admin/users")
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(set(anonymous.json()), {"code", "message", "request_id", "details"})

            injected = client.post(
                "/api/auth/register",
                json={"username": "attacker", "password": PASSWORD, "role": "admin"},
            )
            self.assertEqual(injected.status_code, 422)
            self.assertEqual(injected.json()["code"], "validation_error")

            login = client.post(
                "/api/auth/login", json={"username": "demo_user_a", "password": PASSWORD}
            )
            self.assertEqual(login.status_code, 200)
            set_cookie = login.headers.get_list("set-cookie")
            self.assertTrue(any("HttpOnly" in header for header in set_cookie))
            self.assertTrue(any("SameSite=lax" in header for header in set_cookie))
            forbidden = client.get("/api/admin/users")
            self.assertEqual(forbidden.status_code, 403)

            no_csrf = client.post("/api/auth/logout")
            self.assertEqual(no_csrf.status_code, 403)
            csrf = client.cookies.get("microlens_csrf")
            logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            self.assertEqual(logout.status_code, 204)
            self.assertEqual(client.get("/api/auth/me").status_code, 401)

        with self.factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count(AuthSession.id)).where(AuthSession.revoked_at.is_not(None))
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
