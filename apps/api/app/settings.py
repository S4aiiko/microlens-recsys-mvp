from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _positive_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _exact_origin(value: str) -> str:
    if value == "*":
        raise ValueError("WEB_ORIGIN cannot be wildcard when credentials are enabled")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WEB_ORIGIN must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("WEB_ORIGIN must contain only scheme, host, and optional port")
    return f"{parsed.scheme}://{parsed.netloc}"


def _internal_http_url(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{name} must be a credential-free absolute HTTP origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _search_alias(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value):
        raise ValueError("SEARCH_READ_ALIAS must be a lowercase Elasticsearch alias")
    if value != "microlens-items-read":
        raise ValueError("SEARCH_READ_ALIAS is frozen to microlens-items-read")
    return value


@dataclass(frozen=True)
class AppSettings:
    app_env: str
    database_url: str
    redis_url: str
    jwt_secret: str
    publish_token: str
    web_origin: str
    cookie_secure: bool
    cookie_same_site: str
    cookie_domain: str | None
    session_lifetime: timedelta
    registration_limit: int
    registration_window_seconds: int
    model_artifacts_dir: Path
    processed_data_root: Path
    analytics_exports_dir: Path
    search_url: str
    search_read_alias: str
    alembic_ini: Path
    configured: bool = True

    @classmethod
    def from_environment(cls, *, allow_unconfigured: bool = False) -> AppSettings:
        database_url = os.environ.get("DATABASE_URL")
        redis_url = os.environ.get("REDIS_URL")
        jwt_secret = os.environ.get("JWT_SECRET")
        publish_token = os.environ.get("PUBLISH_TOKEN")
        configured = all((database_url, redis_url, jwt_secret, publish_token))
        if not configured and not allow_unconfigured:
            missing = [
                name
                for name, value in (
                    ("DATABASE_URL", database_url),
                    ("REDIS_URL", redis_url),
                    ("JWT_SECRET", jwt_secret),
                    ("PUBLISH_TOKEN", publish_token),
                )
                if not value
            ]
            raise RuntimeError(f"required runtime settings are missing: {','.join(missing)}")

        same_site = os.environ.get("COOKIE_SAMESITE", "lax").strip().lower()
        if same_site not in {"lax", "strict"}:
            raise ValueError("COOKIE_SAMESITE must be lax or strict")
        root = Path(__file__).resolve().parents[3]
        return cls(
            app_env=os.environ.get("APP_ENV", "development"),
            database_url=database_url or "sqlite+pysqlite:///:memory:",
            redis_url=redis_url or "redis://127.0.0.1:6379/15",
            jwt_secret=jwt_secret or "unconfigured-test-only-secret-at-least-32-bytes",
            publish_token=publish_token or "unconfigured-test-publish-token",
            web_origin=_exact_origin(os.environ.get("WEB_ORIGIN", "http://localhost:5173")),
            cookie_secure=_boolean("COOKIE_SECURE", False),
            cookie_same_site=same_site,
            cookie_domain=os.environ.get("COOKIE_DOMAIN") or None,
            session_lifetime=timedelta(
                seconds=_positive_integer("SESSION_LIFETIME_SECONDS", 28_800)
            ),
            registration_limit=_positive_integer("REGISTRATION_RATE_LIMIT", 5),
            registration_window_seconds=_positive_integer("REGISTRATION_RATE_WINDOW_SECONDS", 300),
            model_artifacts_dir=Path(
                os.environ.get("MODEL_ARTIFACTS_DIR", root / "artifacts" / "models")
            ),
            processed_data_root=Path(
                os.environ.get("PROCESSED_DATA_ROOT", root / "artifacts" / "data")
            ),
            analytics_exports_dir=Path(
                os.environ.get("ANALYTICS_EXPORTS_DIR", root / "artifacts" / "analytics_exports")
            ),
            search_url=_internal_http_url(
                "SEARCH_URL", os.environ.get("SEARCH_URL", "http://search:9200")
            ),
            search_read_alias=_search_alias(
                os.environ.get("SEARCH_READ_ALIAS", "microlens-items-read")
            ),
            alembic_ini=Path(
                os.environ.get("ALEMBIC_INI", root / "apps" / "api" / "alembic" / "alembic.ini")
            ),
            configured=configured,
        )
