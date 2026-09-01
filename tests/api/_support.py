from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth.security import PasswordService, normalize_username
from apps.api.app.db import Base
from apps.api.app.db.models import AccountStatus, Role, User, UserProfile

NOW = datetime(2026, 8, 31, 12, 15, tzinfo=UTC)
PASSWORD = "Correct Horse Battery Staple 2026!"


def _date_trunc(unit: str, raw: str) -> str:
    if unit != "hour":
        raise ValueError("test date_trunc supports only hour")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value.replace(minute=0, second=0, microsecond=0).isoformat(sep=" ")


def sqlite_engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("date_trunc", 2, _date_trunc)

    Base.metadata.create_all(engine)
    return engine


def factory_for(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def add_user(
    session: Session,
    *,
    username: str,
    role: Role = Role.USER,
    password: str = PASSWORD,
    user_id: uuid.UUID | None = None,
) -> User:
    user = User(
        id=user_id or uuid.uuid4(),
        username=username,
        username_normalized=normalize_username(username),
        password_hash=PasswordService().hash(password),
        role=role,
        status=AccountStatus.ENABLED,
    )
    session.add(user)
    session.flush()
    session.add(UserProfile(user_id=user.id))
    session.flush()
    return user
