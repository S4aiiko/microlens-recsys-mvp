from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Engine,
    ForeignKey,
    MetaData,
    String,
    Table,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.session import create_database_engine, create_session_factory

from .domain import (
    READ_ALIAS,
    IndexBuildConflict,
    IndexBuildManifest,
)
from .elasticsearch_adapter import ElasticsearchSearchProjection
from .health import SearchHealthService
from .indexing import FullReindexer, IncrementalIndexer
from .postgres import SqlAlchemyPostgresSearchAuthority
from .service import AuthoritativeSearchService

SEARCH_ADVISORY_LOCK_KEY = 4_812_009_347
SEARCH_RUNTIME_METADATA = MetaData()

SEARCH_INDEX_BUILDS = Table(
    "search_index_builds",
    SEARCH_RUNTIME_METADATA,
    Column("physical_index", String(128), primary_key=True),
    Column("source_version", String(255), nullable=False),
    Column("build_fingerprint", String(64), nullable=False, unique=True),
    Column("document_count", BigInteger, nullable=False),
    Column("projection_checksum", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    Column("activated_at", DateTime(timezone=True)),
    Column("previous_index", String(128)),
)

SEARCH_INDEX_REGISTRY = Table(
    "search_index_registry",
    SEARCH_RUNTIME_METADATA,
    Column("registry_name", String(32), primary_key=True),
    Column("read_alias", String(128), nullable=False, unique=True),
    Column(
        "active_physical_index",
        String(128),
        ForeignKey("search_index_builds.physical_index"),
    ),
    Column("last_source_watermark", String(255)),
    Column("generation", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

SEARCH_INCREMENTAL_RECEIPTS = Table(
    "search_incremental_receipts",
    SEARCH_RUNTIME_METADATA,
    Column("task_key", String(255), primary_key=True),
    Column("input_fingerprint", String(64), nullable=False),
    Column(
        "physical_index",
        String(128),
        ForeignKey("search_index_builds.physical_index"),
        nullable=False,
    ),
    Column("source_watermark", String(255), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
)


class SqlAlchemySearchIndexRegistry:
    """PostgreSQL authority for index builds, activation and incremental receipts."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def get_build(self, physical_index: str) -> IndexBuildManifest | None:
        with self.sessions() as session:
            row = (
                session.execute(
                    select(SEARCH_INDEX_BUILDS).where(
                        SEARCH_INDEX_BUILDS.c.physical_index == physical_index
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _manifest(row) if row is not None else None

    def record_build(self, manifest: IndexBuildManifest) -> None:
        with self.sessions.begin() as session:
            existing = _build_row(session, manifest.physical_index, for_update=True)
            if existing is not None:
                if _manifest(existing) != manifest:
                    raise IndexBuildConflict("physical index already has a different build record")
                return
            fingerprint_owner = session.execute(
                select(SEARCH_INDEX_BUILDS.c.physical_index).where(
                    SEARCH_INDEX_BUILDS.c.build_fingerprint == manifest.build_fingerprint
                )
            ).scalar_one_or_none()
            if fingerprint_owner is not None:
                raise IndexBuildConflict("build fingerprint already belongs to another index")
            session.execute(
                insert(SEARCH_INDEX_BUILDS).values(
                    physical_index=manifest.physical_index,
                    source_version=manifest.source_version,
                    build_fingerprint=manifest.build_fingerprint,
                    document_count=manifest.document_count,
                    projection_checksum=manifest.projection_checksum,
                    status="built",
                    built_at=manifest.built_at.astimezone(UTC),
                    activated_at=None,
                    previous_index=None,
                )
            )

    def mark_active(
        self,
        physical_index: str,
        *,
        previous_index: str | None,
        activated_at: datetime,
    ) -> None:
        event_time = _aware(activated_at)
        with self.sessions.begin() as session:
            target = _build_row(session, physical_index, for_update=True)
            if target is None:
                raise IndexBuildConflict("active index has no authoritative build record")
            active = session.execute(
                select(SEARCH_INDEX_BUILDS.c.physical_index)
                .where(SEARCH_INDEX_BUILDS.c.status == "active")
                .with_for_update()
            ).scalar_one_or_none()
            if active not in {None, physical_index, previous_index}:
                raise IndexBuildConflict("database active index differs from alias precondition")
            registry = (
                session.execute(
                    select(SEARCH_INDEX_REGISTRY)
                    .where(SEARCH_INDEX_REGISTRY.c.registry_name == "items")
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if registry is not None and registry["read_alias"] != READ_ALIAS:
                raise IndexBuildConflict("search registry alias differs from frozen contract")
            if (
                active == physical_index
                and registry is not None
                and registry["active_physical_index"] == physical_index
            ):
                return
            if active is not None and active != physical_index:
                session.execute(
                    update(SEARCH_INDEX_BUILDS)
                    .where(SEARCH_INDEX_BUILDS.c.physical_index == active)
                    .values(status="retired", activated_at=None)
                )
            session.execute(
                update(SEARCH_INDEX_BUILDS)
                .where(SEARCH_INDEX_BUILDS.c.physical_index == physical_index)
                .values(
                    status="active",
                    activated_at=event_time,
                    previous_index=previous_index,
                )
            )
            if registry is None:
                session.execute(
                    insert(SEARCH_INDEX_REGISTRY).values(
                        registry_name="items",
                        read_alias=READ_ALIAS,
                        active_physical_index=physical_index,
                        last_source_watermark=None,
                        generation=1,
                        updated_at=event_time,
                    )
                )
            else:
                session.execute(
                    update(SEARCH_INDEX_REGISTRY)
                    .where(SEARCH_INDEX_REGISTRY.c.registry_name == "items")
                    .values(
                        active_physical_index=physical_index,
                        generation=int(registry["generation"]) + 1,
                        updated_at=event_time,
                    )
                )

    def incremental_fingerprint(self, task_key: str) -> str | None:
        with self.sessions() as session:
            return session.execute(
                select(SEARCH_INCREMENTAL_RECEIPTS.c.input_fingerprint).where(
                    SEARCH_INCREMENTAL_RECEIPTS.c.task_key == task_key
                )
            ).scalar_one_or_none()

    def record_incremental(
        self,
        task_key: str,
        *,
        fingerprint: str,
        physical_index: str,
        source_watermark: str,
        completed_at: datetime,
    ) -> None:
        event_time = _aware(completed_at)
        with self.sessions.begin() as session:
            existing = (
                session.execute(
                    select(SEARCH_INCREMENTAL_RECEIPTS)
                    .where(SEARCH_INCREMENTAL_RECEIPTS.c.task_key == task_key)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["input_fingerprint"] != fingerprint
                    or existing["physical_index"] != physical_index
                    or existing["source_watermark"] != source_watermark
                ):
                    raise IndexBuildConflict("incremental task key has different immutable input")
                return
            if _build_row(session, physical_index, for_update=True) is None:
                raise IndexBuildConflict("incremental target has no authoritative build record")
            session.execute(
                insert(SEARCH_INCREMENTAL_RECEIPTS).values(
                    task_key=task_key,
                    input_fingerprint=fingerprint,
                    physical_index=physical_index,
                    source_watermark=source_watermark,
                    completed_at=event_time,
                )
            )
            registry = (
                session.execute(
                    select(SEARCH_INDEX_REGISTRY)
                    .where(SEARCH_INDEX_REGISTRY.c.registry_name == "items")
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if registry is None or registry["active_physical_index"] != physical_index:
                raise IndexBuildConflict("incremental target is not the database active index")
            session.execute(
                update(SEARCH_INDEX_REGISTRY)
                .where(SEARCH_INDEX_REGISTRY.c.registry_name == "items")
                .values(
                    last_source_watermark=source_watermark,
                    generation=int(registry["generation"]) + 1,
                    updated_at=event_time,
                )
            )

    def last_source_watermark(self) -> str | None:
        with self.sessions() as session:
            return session.execute(
                select(SEARCH_INDEX_REGISTRY.c.last_source_watermark).where(
                    SEARCH_INDEX_REGISTRY.c.registry_name == "items"
                )
            ).scalar_one_or_none()


SpecT = TypeVar("SpecT")
ResultT = TypeVar("ResultT")


class Runner(Protocol[SpecT, ResultT]):
    def run(self, spec: SpecT) -> ResultT: ...


class PostgresSerializedRunner:
    """Hold one PostgreSQL advisory transaction lock across a projection mutation."""

    _fallback_lock = threading.Lock()

    def __init__(self, engine: Engine, runner: Runner[Any, Any]) -> None:
        self.engine = engine
        self.runner = runner

    def run(self, spec: Any) -> Any:
        if self.engine.dialect.name != "postgresql":
            # Lightweight unit databases do not prove cross-process serialization.
            with self._fallback_lock:
                return self.runner.run(spec)
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": SEARCH_ADVISORY_LOCK_KEY},
            )
            return self.runner.run(spec)


@dataclass(frozen=True)
class SearchRuntime:
    service: AuthoritativeSearchService
    health_service: SearchHealthService
    registry: SqlAlchemySearchIndexRegistry
    full_reindexer: PostgresSerializedRunner
    incremental_indexer: PostgresSerializedRunner


def build_search_runtime(
    *,
    engine: Engine,
    sessions: sessionmaker[Session],
    search_url: str,
    search_read_alias: str,
) -> SearchRuntime:
    if search_read_alias != READ_ALIAS:
        raise ValueError(f"SEARCH_READ_ALIAS must equal {READ_ALIAS}")
    projection = ElasticsearchSearchProjection.from_url(search_url)
    authority = SqlAlchemyPostgresSearchAuthority(sessions)
    registry = SqlAlchemySearchIndexRegistry(sessions)

    def clock() -> datetime:
        return datetime.now(UTC)

    return SearchRuntime(
        service=AuthoritativeSearchService(projection, authority),
        health_service=SearchHealthService(projection, authority, registry),
        registry=registry,
        full_reindexer=PostgresSerializedRunner(
            engine,
            FullReindexer(projection, authority, registry, clock=clock),
        ),
        incremental_indexer=PostgresSerializedRunner(
            engine,
            IncrementalIndexer(projection, authority, registry, clock=clock),
        ),
    )


def build_full_reindexer_from_environment() -> PostgresSerializedRunner:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    runtime = build_search_runtime(
        engine=engine,
        sessions=create_session_factory(engine),
        search_url=os.environ.get("SEARCH_URL", "http://search:9200"),
        search_read_alias=os.environ.get("SEARCH_READ_ALIAS", READ_ALIAS),
    )
    return runtime.full_reindexer


def _build_row(session: Session, physical_index: str, *, for_update: bool) -> dict[str, Any] | None:
    statement = select(SEARCH_INDEX_BUILDS).where(
        SEARCH_INDEX_BUILDS.c.physical_index == physical_index
    )
    if for_update:
        statement = statement.with_for_update()
    row = session.execute(statement).mappings().one_or_none()
    return dict(row) if row is not None else None


def _manifest(row: Any) -> IndexBuildManifest:
    return IndexBuildManifest(
        physical_index=row["physical_index"],
        source_version=row["source_version"],
        build_fingerprint=row["build_fingerprint"],
        document_count=int(row["document_count"]),
        projection_checksum=row["projection_checksum"],
        built_at=ensure_utc(row["built_at"]),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
