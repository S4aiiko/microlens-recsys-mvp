from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pwdlib import PasswordHash

SESSION_COOKIE_NAME = "microlens_session"
CSRF_COOKIE_NAME = "microlens_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def normalize_username(username: str) -> str:
    value = unicodedata.normalize("NFKC", username).strip().casefold()
    if len(value) < 3 or len(value) > 64:
        raise ValueError("username must contain between 3 and 64 normalized characters")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("username cannot contain whitespace or control characters")
    return value


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()

    def hash(self, password: str) -> str:
        if len(password) < 12 or len(password) > 256:
            raise ValueError("password must contain between 12 and 256 characters")
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._hasher.verify(password, password_hash)


@dataclass(frozen=True)
class CookieSettings:
    secure: bool
    same_site: Literal["lax", "strict"] = "lax"
    domain: str | None = None
    path: str = "/"


@dataclass(frozen=True)
class JWTSettings:
    secret: str
    issuer: str = "microlens-api"
    audience: str = "microlens-web"
    lifetime: timedelta = timedelta(hours=8)
    algorithm: Literal["HS256"] = "HS256"

    def __post_init__(self) -> None:
        if len(self.secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes")
        if self.lifetime <= timedelta(0):
            raise ValueError("JWT lifetime must be positive")


@dataclass(frozen=True)
class SessionToken:
    token: str
    jti: str
    csrf_token: str
    csrf_digest: str
    expires_at: datetime


@dataclass(frozen=True)
class DecodedToken:
    user_id: uuid.UUID
    jti: str
    expires_at: datetime


class JWTService:
    def __init__(self, settings: JWTSettings) -> None:
        self.settings = settings

    def issue(self, user_id: uuid.UUID, *, now: datetime | None = None) -> SessionToken:
        issued_at = (now or datetime.now(UTC)).astimezone(UTC)
        expires_at = issued_at + self.settings.lifetime
        jti = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        csrf_digest = digest_csrf(csrf_token)
        claims = {
            "sub": str(user_id),
            "jti": jti,
            "iat": issued_at,
            "exp": expires_at,
            "iss": self.settings.issuer,
            "aud": self.settings.audience,
        }
        token = jwt.encode(claims, self.settings.secret, algorithm=self.settings.algorithm)
        return SessionToken(token, jti, csrf_token, csrf_digest, expires_at)

    def decode(self, token: str, *, now: datetime | None = None) -> DecodedToken:
        options = {"require": ["sub", "jti", "iat", "exp", "iss", "aud"]}
        if now is None:
            claims = jwt.decode(
                token,
                self.settings.secret,
                algorithms=[self.settings.algorithm],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options=options,
            )
        else:
            # PyJWT validates against wall clock; this explicit check keeps tests deterministic.
            claims = jwt.decode(
                token,
                self.settings.secret,
                algorithms=[self.settings.algorithm],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={**options, "verify_exp": False},
            )
        expires_at = datetime.fromtimestamp(float(claims["exp"]), UTC)
        check_now = (now or datetime.now(UTC)).astimezone(UTC)
        if expires_at <= check_now:
            raise jwt.ExpiredSignatureError("session token expired")
        return DecodedToken(
            user_id=uuid.UUID(str(claims["sub"])),
            jti=str(claims["jti"]),
            expires_at=expires_at,
        )


def digest_csrf(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_matches(cookie_token: str, header_token: str, expected_digest: str) -> bool:
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token) and hmac.compare_digest(
        digest_csrf(cookie_token), expected_digest
    )
