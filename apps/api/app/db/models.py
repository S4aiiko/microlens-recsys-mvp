from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utc_now


class Role(enum.StrEnum):
    USER = "user"
    OPERATOR_READONLY = "operator_readonly"
    OPERATOR = "operator"
    ADMIN = "admin"


class AccountStatus(enum.StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class FeedType(enum.StrEnum):
    PERSONALIZED = "personalized"
    POPULAR = "popular"
    EXPLORE = "explore"


class EventType(enum.StrEnum):
    IMPRESSION = "impression"
    CLICK = "click"
    LIKE = "like"
    NOT_INTERESTED = "not_interested"
    DWELL = "dwell"
    REVISIT = "revisit"
    SHARE = "share"


class OnlineStatus(enum.StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class OperationType(enum.StrEnum):
    PROMOTE = "promote"
    OFFLINE = "offline"
    RESTORE = "restore"


class OperationBatchStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PromotionStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ScopeType(enum.StrEnum):
    ALL = "all"
    USER = "user"
    FEED = "feed"


class ModelStatus(enum.StrEnum):
    TRAINING = "TRAINING"
    EVALUATED = "EVALUATED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class TrainingJobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationPurpose(enum.StrEnum):
    BASE_OFFICIAL = "base_official"
    SYSTEMS_ONLY = "systems_only"
    QUALITY_EVALUATION = "quality_evaluation"


class Comparability(enum.StrEnum):
    NON_COMPARABLE = "non_comparable"
    COMPARABLE = "comparable"


def enum_type(enum_class: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda values: [member.value for member in values],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[Role] = mapped_column(enum_type(Role, "role"), nullable=False, default=Role.USER)
    source_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(
        enum_type(AccountStatus, "account_status"), nullable=False, default=AccountStatus.ENABLED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint("length(username) >= 3", name="username_min_length"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Item(Base):
    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    likes_snapshot: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    views_snapshot: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cover_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_status: Mapped[str] = mapped_column(String(64), nullable=False, default="complete")
    online_status: Mapped[OnlineStatus] = mapped_column(
        enum_type(OnlineStatus, "online_status"), nullable=False, default=OnlineStatus.ONLINE
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (CheckConstraint("state_version >= 0", name="state_version_nonnegative"),)


class RecommendationSnapshot(Base):
    __tablename__ = "recommendation_snapshots"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feed_type: Mapped[FeedType] = mapped_column(
        enum_type(FeedType, "feed_type"), nullable=False, index=True
    )
    model_version: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class RecommendationRequest(Base):
    __tablename__ = "recommendation_requests"

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_snapshots.snapshot_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    offset: Mapped[int] = mapped_column(Integer, nullable=False)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        CheckConstraint('"offset" >= 0', name="offset_nonnegative"),
        CheckConstraint('"limit" > 0 AND "limit" <= 100', name="limit_range"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
    )


class RecommendationSnapshotItem(Base):
    __tablename__ = "recommendation_snapshot_items"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    normalized_score: Mapped[float] = mapped_column(Float, nullable=False)
    filter_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snapshot_position: Mapped[int] = mapped_column(Integer, nullable=False)
    promotion_rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    __table_args__ = (
        UniqueConstraint("snapshot_id", "snapshot_position", name="uq_snapshot_item_position"),
        CheckConstraint("snapshot_position >= 0", name="snapshot_position_nonnegative"),
    )


class Exposure(Base):
    __tablename__ = "exposures"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_requests.request_id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(255), nullable=False)
    exposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )

    __table_args__ = (
        UniqueConstraint("request_id", "item_id", "position", name="uq_exposure_request_item_pos"),
        UniqueConstraint(
            "id",
            "request_id",
            "user_id",
            "item_id",
            "position",
            name="uq_exposure_event_identity",
        ),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    exposure_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_requests.request_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    feed_type: Mapped[FeedType] = mapped_column(
        enum_type(FeedType, "event_feed_type"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[EventType] = mapped_column(
        enum_type(EventType, "event_type"), nullable=False, index=True
    )
    client_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["exposure_id", "request_id", "user_id", "item_id", "position"],
            [
                "exposures.id",
                "exposures.request_id",
                "exposures.user_id",
                "exposures.item_id",
                "exposures.position",
            ],
            name="fk_events_exposure_identity",
            ondelete="CASCADE",
        ),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint(
            "duration_ms IS NULL OR (duration_ms >= 0 AND duration_ms <= 86400000)",
            name="duration_range",
        ),
        CheckConstraint(
            "(event_type = 'impression' AND exposure_id IS NOT NULL) OR "
            "(event_type <> 'impression')",
            name="impression_has_exposure",
        ),
        Index(
            "uq_events_canonical_impression_exposure",
            "exposure_id",
            unique=True,
            postgresql_where=text("event_type = 'impression'"),
            sqlite_where=text("event_type = 'impression'"),
        ),
        Index("ix_events_request_item", "request_id", "item_id"),
    )


class EventBatch(Base):
    __tablename__ = "event_batches"

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint("item_count > 0 AND item_count <= 100", name="item_count_range"),
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    recent_interactions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    positive_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    negative_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dwell_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    revisit_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    share_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    title_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (CheckConstraint("profile_version >= 0", name="profile_version_nonnegative"),)


class PromotionRule(Base):
    __tablename__ = "promotion_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    scope_type: Mapped[ScopeType] = mapped_column(
        enum_type(ScopeType, "scope_type"), nullable=False
    )
    scope_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[PromotionStatus] = mapped_column(
        enum_type(PromotionStatus, "promotion_status"), nullable=False
    )
    operation_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_batches.batch_id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="valid_window"),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint(
            "target_position IS NULL OR target_position >= 0", name="position_nonnegative"
        ),
        CheckConstraint(
            "(scope_type = 'all' AND scope_value IS NULL) OR "
            "(scope_type <> 'all' AND scope_value IS NOT NULL)",
            name="scope_value_shape",
        ),
    )


class OperationBatch(Base):
    __tablename__ = "operation_batches"

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    operator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operator_role: Mapped[Role] = mapped_column(
        enum_type(Role, "operation_operator_role"), nullable=False
    )
    operation_type: Mapped[OperationType] = mapped_column(
        enum_type(OperationType, "operation_type"), nullable=False
    )
    targets: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    expected_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[OperationBatchStatus] = mapped_column(
        enum_type(OperationBatchStatus, "operation_batch_status"), nullable=False
    )
    scope_type: Mapped[ScopeType | None] = mapped_column(
        enum_type(ScopeType, "operation_scope_type"), nullable=True
    )
    scope_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (
        CheckConstraint("json_array_length(targets) BETWEEN 1 AND 100", name="target_count_range"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="valid_window"),
        CheckConstraint("expected_state_version >= 0", name="expected_version_nonnegative"),
    )


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_batches.batch_id", ondelete="CASCADE"), nullable=False, index=True
    )
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("result IN ('succeeded', 'failed')", name="result_value"),)


class ModelVersion(Base):
    __tablename__ = "model_versions"

    model_version: Mapped[str] = mapped_column(String(255), primary_key=True)
    data_version: Mapped[str] = mapped_column(String(255), nullable=False)
    config_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[EvaluationPurpose] = mapped_column(
        enum_type(EvaluationPurpose, "evaluation_purpose"), nullable=False
    )
    evaluation_comparability: Mapped[Comparability] = mapped_column(
        enum_type(Comparability, "evaluation_comparability"), nullable=False
    )
    activation_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[ModelStatus] = mapped_column(
        enum_type(ModelStatus, "model_status"), nullable=False, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("length(config_checksum) = 64", name="config_checksum_length"),
        CheckConstraint("length(artifact_checksum) = 64", name="artifact_checksum_length"),
        CheckConstraint("length(manifest_checksum) = 64", name="manifest_checksum_length"),
        CheckConstraint(
            "NOT activation_eligible OR evaluation_comparability = 'comparable'",
            name="eligible_requires_comparable",
        ),
        CheckConstraint(
            "purpose <> 'systems_only' OR "
            "(evaluation_comparability = 'non_comparable' AND NOT activation_eligible "
            "AND status NOT IN ('READY', 'ACTIVE'))",
            name="systems_only_never_activates",
        ),
        Index(
            "uq_model_versions_single_active",
            text("(1)"),
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )


class TrainingJob(Base):
    __tablename__ = "training_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    data_version: Mapped[str] = mapped_column(String(255), nullable=False)
    data_manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    config_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[EvaluationPurpose] = mapped_column(
        enum_type(EvaluationPurpose, "training_purpose"), nullable=False
    )
    evaluation_comparability: Mapped[Comparability] = mapped_column(
        enum_type(Comparability, "training_comparability"), nullable=False
    )
    activation_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[TrainingJobStatus] = mapped_column(
        enum_type(TrainingJobStatus, "training_job_status"), nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "purpose <> 'systems_only' OR "
            "(evaluation_comparability = 'non_comparable' AND NOT activation_eligible)",
            name="systems_only_noncomparable_ineligible",
        ),
        CheckConstraint(
            "NOT activation_eligible OR evaluation_comparability = 'comparable'",
            name="eligible_requires_comparable",
        ),
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("training_jobs.job_id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_job_attempt_number"),
        CheckConstraint("attempt > 0", name="attempt_positive"),
    )


class ModelActivationAttempt(Base):
    __tablename__ = "model_activation_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_version: Mapped[str] = mapped_column(
        ForeignKey("model_versions.model_version", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expected_current_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('started', 'succeeded', 'failed')", name="status_value"),
        CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND failure_reason IS NOT NULL) "
            "OR (status <> 'failed' AND failure_code IS NULL AND failure_reason IS NULL)",
            name="failure_shape",
        ),
    )


class TrainingExportWatermark(Base):
    __tablename__ = "training_export_watermarks"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expected_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint("last_event_id >= 0", name="last_event_id_nonnegative"),
        CheckConstraint(
            "status IN ('idle', 'exporting', 'completed', 'failed')", name="status_value"
        ),
    )
