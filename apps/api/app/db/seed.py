from __future__ import annotations

import os
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountStatus, Role, User, UserProfile

SEED_NAMESPACE = uuid.UUID("89036c6b-8b39-4fc1-9df5-2689e36dd2ac")
SEED_USERS: tuple[tuple[str, Role], ...] = (
    ("demo_user_a", Role.USER),
    ("demo_user_b", Role.USER),
    ("demo_user_c", Role.USER),
    ("operator_readonly", Role.OPERATOR_READONLY),
    ("operator", Role.OPERATOR),
    ("admin", Role.ADMIN),
)


def seed_demo_users(
    session: Session,
    *,
    password: str,
    hash_password: Callable[[str], str],
    normalize_username: Callable[[str], str],
) -> list[User]:
    """Idempotently add demo identities without embedding or logging a credential."""

    if len(password) < 12:
        raise ValueError("seed password must contain at least 12 characters")
    normalized = [normalize_username(username) for username, _ in SEED_USERS]
    existing = {
        user.username_normalized: user
        for user in session.scalars(select(User).where(User.username_normalized.in_(normalized)))
    }
    result: list[User] = []
    for username, role in SEED_USERS:
        key = normalize_username(username)
        expected_id = uuid.uuid5(SEED_NAMESPACE, key)
        user = existing.get(key)
        if user is None:
            user = User(
                id=expected_id,
                username=username,
                username_normalized=key,
                password_hash=hash_password(password),
                role=role,
                status=AccountStatus.ENABLED,
            )
            session.add(user)
            session.add(UserProfile(user_id=user.id))
            existing[key] = user
        else:
            mismatches: list[str] = []
            if user.id != expected_id:
                mismatches.append("id")
            if user.role != role:
                mismatches.append("role")
            if user.status != AccountStatus.ENABLED:
                mismatches.append("status")
            if mismatches:
                raise ValueError(f"seed identity {username} conflicts on {','.join(mismatches)}")
            if session.get(UserProfile, user.id) is None:
                session.add(UserProfile(user_id=user.id))
        result.append(user)
    session.flush()
    return result


def seed_password_from_environment() -> str:
    value = os.environ.get("MICROLENS_SEED_PASSWORD")
    if value is None:
        raise RuntimeError("MICROLENS_SEED_PASSWORD is required for the demo seed")
    if len(value) < 12:
        raise RuntimeError("MICROLENS_SEED_PASSWORD must contain at least 12 characters")
    return value
