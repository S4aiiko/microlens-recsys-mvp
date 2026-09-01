"""Create Phase 2B core tables explicitly.

Revision ID: 20260901_0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0001"
down_revision = None
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("username_normalized", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role",
            _enum("user", "operator_readonly", "operator", "admin", name="role"),
            nullable=False,
        ),
        sa.Column("source_user_id", sa.String(255), nullable=True),
        sa.Column("status", _enum("enabled", "disabled", name="account_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(username) >= 3", name="ck_users_username_min_length"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("username_normalized", name="uq_users_username_normalized"),
    )
    op.create_table(
        "items",
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("likes_snapshot", sa.BigInteger(), nullable=True),
        sa.Column("views_snapshot", sa.BigInteger(), nullable=True),
        sa.Column("cover_ref", sa.Text(), nullable=True),
        sa.Column("metadata_status", sa.String(64), nullable=False),
        sa.Column(
            "online_status", _enum("online", "offline", name="online_status"), nullable=False
        ),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state_version >= 0", name="ck_items_state_version_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_items"),
    )
    op.create_table(
        "model_versions",
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("data_version", sa.String(255), nullable=False),
        sa.Column("config_checksum", sa.String(64), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_checksum", sa.String(64), nullable=False),
        sa.Column("manifest_checksum", sa.String(64), nullable=False),
        sa.Column(
            "purpose",
            _enum("base_official", "systems_only", "quality_evaluation", name="evaluation_purpose"),
            nullable=False,
        ),
        sa.Column(
            "evaluation_comparability",
            _enum("non_comparable", "comparable", name="evaluation_comparability"),
            nullable=False,
        ),
        sa.Column("activation_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "TRAINING",
                "EVALUATED",
                "READY",
                "ACTIVE",
                "ARCHIVED",
                "FAILED",
                name="model_status",
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(config_checksum) = 64", name="ck_model_versions_config_checksum_length"
        ),
        sa.CheckConstraint(
            "length(artifact_checksum) = 64", name="ck_model_versions_artifact_checksum_length"
        ),
        sa.CheckConstraint(
            "length(manifest_checksum) = 64", name="ck_model_versions_manifest_checksum_length"
        ),
        sa.CheckConstraint(
            "NOT activation_eligible OR evaluation_comparability = 'comparable'",
            name="ck_model_versions_eligible_requires_comparable",
        ),
        sa.CheckConstraint(
            "purpose <> 'systems_only' OR (evaluation_comparability = 'non_comparable' "
            "AND NOT activation_eligible AND status NOT IN ('READY', 'ACTIVE'))",
            name="ck_model_versions_systems_only_never_activates",
        ),
        sa.PrimaryKeyConstraint("model_version", name="pk_model_versions"),
    )
    op.create_table(
        "training_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("data_version", sa.String(255), nullable=False),
        sa.Column("data_manifest_checksum", sa.String(64), nullable=False),
        sa.Column("config_checksum", sa.String(64), nullable=False),
        sa.Column(
            "purpose",
            _enum("base_official", "systems_only", "quality_evaluation", name="training_purpose"),
            nullable=False,
        ),
        sa.Column(
            "evaluation_comparability",
            _enum("non_comparable", "comparable", name="training_comparability"),
            nullable=False,
        ),
        sa.Column("activation_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "queued", "running", "succeeded", "failed", "cancelled", name="training_job_status"
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id", name="pk_training_jobs"),
        sa.UniqueConstraint("idempotency_key", name="uq_training_jobs_idempotency_key"),
    )
    op.create_table(
        "training_export_watermarks",
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_checksum", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "last_event_id >= 0", name="ck_training_export_watermarks_last_event_id_nonnegative"
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'exporting', 'completed', 'failed')",
            name="ck_training_export_watermarks_status_value",
        ),
        sa.PrimaryKeyConstraint("name", name="pk_training_export_watermarks"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_auth_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("jti", name="uq_auth_sessions_jti"),
    )
    op.create_table(
        "recommendation_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "feed_type",
            _enum("personalized", "popular", "explore", name="feed_type"),
            nullable=False,
        ),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("snapshot_seed", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_recommendation_snapshots_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name="pk_recommendation_snapshots"),
    )
    op.create_table(
        "recommendation_requests",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("limit", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('"offset" >= 0', name="ck_recommendation_requests_offset_nonnegative"),
        sa.CheckConstraint(
            '"limit" > 0 AND "limit" <= 100',
            name="ck_recommendation_requests_limit_range",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_recommendation_requests_latency_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["recommendation_snapshots.snapshot_id"],
            name="fk_recommendation_requests_snapshot_id_recommendation_snapshots",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_recommendation_requests_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("request_id", name="pk_recommendation_requests"),
    )
    op.create_table(
        "recommendation_snapshot_items",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("raw_score", sa.Float(), nullable=False),
        sa.Column("normalized_score", sa.Float(), nullable=False),
        sa.Column("filter_reason", sa.String(255), nullable=True),
        sa.Column("snapshot_position", sa.Integer(), nullable=False),
        sa.Column("promotion_rule_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "snapshot_position >= 0",
            name="ck_recommendation_snapshot_items_snapshot_position_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name="fk_recommendation_snapshot_items_item_id_items",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["recommendation_snapshots.snapshot_id"],
            name="fk_rec_snapshot_items_snapshot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "item_id", name="pk_recommendation_snapshot_items"),
        sa.UniqueConstraint("snapshot_id", "snapshot_position", name="uq_snapshot_item_position"),
    )
    op.create_table(
        "exposures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("exposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_exposures_position_nonnegative"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name="fk_exposures_item_id_items", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["recommendation_requests.request_id"],
            name="fk_exposures_request_id_recommendation_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["recommendation_snapshots.snapshot_id"],
            name="fk_exposures_snapshot_id_recommendation_snapshots",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_exposures_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exposures"),
        sa.UniqueConstraint(
            "request_id", "item_id", "position", name="uq_exposure_request_item_pos"
        ),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("exposure_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "feed_type",
            _enum("personalized", "popular", "explore", name="event_feed_type"),
            nullable=False,
        ),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column(
            "event_type",
            _enum(
                "impression",
                "click",
                "like",
                "not_interested",
                "dwell",
                "revisit",
                "share",
                name="event_type",
            ),
            nullable=False,
        ),
        sa.Column("client_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("server_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_events_position_nonnegative"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR (duration_ms >= 0 AND duration_ms <= 86400000)",
            name="ck_events_duration_range",
        ),
        sa.CheckConstraint(
            "(event_type = 'impression' AND exposure_id IS NOT NULL) OR "
            "(event_type <> 'impression')",
            name="ck_events_impression_has_exposure",
        ),
        sa.ForeignKeyConstraint(
            ["exposure_id"],
            ["exposures.id"],
            name="fk_events_exposure_id_exposures",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name="fk_events_item_id_items", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["recommendation_requests.request_id"],
            name="fk_events_request_id_recommendation_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_events_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("event_id", name="uq_events_event_id"),
    )
    op.create_table(
        "event_batches",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "item_count > 0 AND item_count <= 100", name="ck_event_batches_item_count_range"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_event_batches_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("batch_id", name="pk_event_batches"),
    )
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("recent_interactions", sa.JSON(), nullable=False),
        sa.Column("positive_summary", sa.JSON(), nullable=False),
        sa.Column("negative_summary", sa.JSON(), nullable=False),
        sa.Column("dwell_summary", sa.JSON(), nullable=False),
        sa.Column("revisit_summary", sa.JSON(), nullable=False),
        sa.Column("share_summary", sa.JSON(), nullable=False),
        sa.Column("title_preferences", sa.JSON(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "profile_version >= 0", name="ck_user_profiles_profile_version_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_profiles_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_profiles"),
    )
    op.create_table(
        "operation_batches",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column(
            "operation_type",
            _enum("promote", "offline", "restore", name="operation_type"),
            nullable=False,
        ),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("expected_state_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum("scheduled", "running", "succeeded", "failed", name="operation_batch_status"),
            nullable=False,
        ),
        sa.Column(
            "scope_type", _enum("all", "user", "feed", name="operation_scope_type"), nullable=True
        ),
        sa.Column("scope_value", sa.String(255), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "json_array_length(targets) BETWEEN 1 AND 100",
            name="ck_operation_batches_target_count_range",
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at", name="ck_operation_batches_valid_window"
        ),
        sa.CheckConstraint(
            "expected_state_version >= 0", name="ck_operation_batches_expected_version_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["users.id"],
            name="fk_operation_batches_operator_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("batch_id", name="pk_operation_batches"),
    )
    op.create_table(
        "promotion_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scope_type", _enum("all", "user", "feed", name="scope_type"), nullable=False),
        sa.Column("scope_value", sa.String(255), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "status",
            _enum("scheduled", "active", "expired", "cancelled", name="promotion_status"),
            nullable=False,
        ),
        sa.Column("operation_batch_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at", name="ck_promotion_rules_valid_window"
        ),
        sa.CheckConstraint("priority >= 0", name="ck_promotion_rules_priority_nonnegative"),
        sa.CheckConstraint(
            "target_position IS NULL OR target_position >= 0",
            name="ck_promotion_rules_position_nonnegative",
        ),
        sa.CheckConstraint(
            "(scope_type = 'all' AND scope_value IS NULL) OR "
            "(scope_type <> 'all' AND scope_value IS NOT NULL)",
            name="ck_promotion_rules_scope_value_shape",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_promotion_rules_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name="fk_promotion_rules_item_id_items", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_batch_id"],
            ["operation_batches.batch_id"],
            name="fk_promotion_rules_operation_batch_id_operation_batches",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotion_rules"),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("before_value", sa.JSON(), nullable=True),
        sa.Column("after_value", sa.JSON(), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("result IN ('succeeded', 'failed')", name="ck_operations_result_value"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["operation_batches.batch_id"],
            name="fk_operations_batch_id_operation_batches",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operations"),
    )
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("attempt > 0", name="ck_job_attempts_attempt_positive"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["training_jobs.job_id"],
            name="fk_job_attempts_job_id_training_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_attempts"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_job_attempt_number"),
    )


def downgrade() -> None:
    for table in (
        "job_attempts",
        "operations",
        "promotion_rules",
        "operation_batches",
        "user_profiles",
        "event_batches",
        "events",
        "exposures",
        "recommendation_snapshot_items",
        "recommendation_requests",
        "recommendation_snapshots",
        "auth_sessions",
        "training_export_watermarks",
        "training_jobs",
        "model_versions",
        "items",
        "users",
    ):
        op.drop_table(table)
