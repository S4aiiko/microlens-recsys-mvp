from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import AccountStatus, AuthSession, Role, User, UserProfile

from .errors import ApiError
from .security import JWTService, PasswordService, SessionToken, normalize_username


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    session: AuthSession


class AuthService:
    def __init__(self, passwords: PasswordService, tokens: JWTService) -> None:
        self.passwords = passwords
        self.tokens = tokens

    def register(self, session: Session, username: str, password: str) -> User:
        try:
            normalized = normalize_username(username)
            password_hash = self.passwords.hash(password)
        except ValueError as exc:
            raise ApiError(422, "invalid_registration", str(exc)) from exc
        if session.scalar(select(User.id).where(User.username_normalized == normalized)):
            raise ApiError(409, "username_exists", "Username is already registered")
        user = User(
            username=unicoded_display_username(username),
            username_normalized=normalized,
            password_hash=password_hash,
            role=Role.USER,
            status=AccountStatus.ENABLED,
        )
        session.add(user)
        try:
            session.flush()
        except IntegrityError as exc:
            raise ApiError(409, "username_exists", "Username is already registered") from exc
        session.add(UserProfile(user_id=user.id))
        session.flush()
        return user

    def login(
        self, session: Session, username: str, password: str, *, now: datetime | None = None
    ) -> tuple[User, SessionToken]:
        try:
            normalized = normalize_username(username)
        except ValueError as exc:
            raise ApiError(401, "invalid_credentials", "Invalid username or password") from exc
        user = session.scalar(select(User).where(User.username_normalized == normalized))
        if (
            user is None
            or user.status != AccountStatus.ENABLED
            or not self.passwords.verify(password, user.password_hash)
        ):
            raise ApiError(401, "invalid_credentials", "Invalid username or password")
        issued = self.tokens.issue(user.id, now=now)
        session.add(
            AuthSession(
                user_id=user.id,
                jti=issued.jti,
                csrf_digest=issued.csrf_digest,
                expires_at=issued.expires_at,
            )
        )
        session.flush()
        return user, issued

    def authenticate(
        self, session: Session, token: str | None, *, now: datetime | None = None
    ) -> AuthenticatedUser:
        if not token:
            raise ApiError(401, "authentication_required", "Authentication is required")
        try:
            decoded = self.tokens.decode(token, now=now)
        except Exception as exc:
            raise ApiError(401, "invalid_session", "Session is invalid or expired") from exc
        auth_session = session.scalar(
            select(AuthSession).where(
                AuthSession.jti == decoded.jti,
                AuthSession.user_id == decoded.user_id,
            )
        )
        check_now = (now or datetime.now(UTC)).astimezone(UTC)
        if (
            auth_session is None
            or auth_session.revoked_at is not None
            or ensure_utc(auth_session.expires_at) <= check_now
        ):
            raise ApiError(401, "invalid_session", "Session is invalid or expired")
        user = session.get(User, decoded.user_id)
        if user is None or user.status != AccountStatus.ENABLED:
            raise ApiError(401, "invalid_session", "Session is invalid or expired")
        return AuthenticatedUser(user=user, session=auth_session)

    def revoke(
        self, session: Session, authenticated: AuthenticatedUser, *, now: datetime | None = None
    ) -> None:
        authenticated.session.revoked_at = (now or datetime.now(UTC)).astimezone(UTC)
        session.flush()


def unicoded_display_username(username: str) -> str:
    value = username.strip()
    if len(value) < 3 or len(value) > 64:
        raise ApiError(422, "invalid_username", "Username must contain 3 to 64 characters")
    return value
