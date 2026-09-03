"""Add the durable async, alert, operation and search runtime schema.

Revision ID: 20260902_0004
Revises: 20260901_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0004"
down_revision = "20260901_0003"
branch_labels = None
depends_on = None


def _sha256_check(column: str) -> str:
    return f"{column} ~ '^[0-9a-f]{{64}}$'"


def _finite_float_check(column: str) -> str:
    return (
        f"{column} NOT IN "
        "('NaN'::double precision, 'Infinity'::double precision, "
        "'-Infinity'::double precision)"
    )


def upgrade() -> None:
    op.create_table(
        "async_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("task_name", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_async_jobs_status_value",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 100", name="ck_async_jobs_max_attempts_range"
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts",
            name="ck_async_jobs_attempt_count_range",
        ),
        sa.CheckConstraint(
            _sha256_check("payload_fingerprint"),
            name="ck_async_jobs_payload_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (completed_at IS NOT NULL)",
            name="ck_async_jobs_terminal_completion_shape",
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR result IS NOT NULL",
            name="ck_async_jobs_succeeded_result_required",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_async_jobs"),
        sa.UniqueConstraint("idempotency_key", name="uq_async_jobs_idempotency_key"),
    )
    op.create_index(
        "ix_async_jobs_queued_due",
        "async_jobs",
        ["due_at", "created_at", "job_id"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )

    op.create_table(
        "async_job_attempts",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("fence_token", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt > 0", name="ck_async_job_attempts_attempt_positive"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'lease_expired', 'cancelled')",
            name="ck_async_job_attempts_status_value",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL) OR "
            "(status <> 'running' AND lease_expires_at IS NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_async_job_attempts_lease_completion_shape",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["async_jobs.job_id"],
            name="fk_async_job_attempts_job_id_async_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_async_job_attempts"),
        sa.UniqueConstraint("fence_token", name="uq_async_job_attempts_fence_token"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_async_job_attempts_job_id_attempt"),
    )
    op.create_index(
        "ix_async_job_attempts_running_lease",
        "async_job_attempts",
        ["lease_expires_at", "attempt_id"],
        unique=False,
        postgresql_where=sa.text("status = 'running' AND lease_expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_async_job_attempts_job_id",
        "async_job_attempts",
        ["job_id"],
        unique=False,
    )

    op.create_table(
        "async_outbox",
        sa.Column("outbox_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("delivery_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'published')",
            name="ck_async_outbox_status_value",
        ),
        sa.CheckConstraint(
            "delivery_attempts >= 0", name="ck_async_outbox_delivery_attempts_nonnegative"
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND delivery_token IS NULL AND lease_expires_at IS NULL "
            "AND published_at IS NULL) OR "
            "(status = 'delivering' AND delivery_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND published_at IS NULL) OR "
            "(status = 'published' AND delivery_token IS NULL AND lease_expires_at IS NULL "
            "AND published_at IS NOT NULL)",
            name="ck_async_outbox_delivery_shape",
        ),
        sa.PrimaryKeyConstraint("outbox_id", name="pk_async_outbox"),
        sa.UniqueConstraint("delivery_token", name="uq_async_outbox_delivery_token"),
        sa.UniqueConstraint("idempotency_key", name="uq_async_outbox_idempotency_key"),
    )
    op.create_index(
        "ix_async_outbox_pending_due",
        "async_outbox",
        ["available_at", "outbox_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_async_outbox_delivering_lease",
        "async_outbox",
        ["lease_expires_at", "outbox_id"],
        unique=False,
        postgresql_where=sa.text("status = 'delivering' AND lease_expires_at IS NOT NULL"),
    )

    op.create_table(
        "alert_rules",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("comparator", sa.String(8), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("min_samples", sa.Integer(), nullable=False),
        sa.Column("aggregation", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "comparator IN ('gt', 'gte', 'lt', 'lte')",
            name="ck_alert_rules_comparator_value",
        ),
        sa.CheckConstraint(
            "aggregation IN ('sum', 'avg', 'min', 'max', 'count')",
            name="ck_alert_rules_aggregation_value",
        ),
        sa.CheckConstraint(
            _finite_float_check("threshold"), name="ck_alert_rules_threshold_finite"
        ),
        sa.CheckConstraint("window_seconds > 0", name="ck_alert_rules_window_seconds_positive"),
        sa.CheckConstraint("min_samples > 0", name="ck_alert_rules_min_samples_positive"),
        sa.PrimaryKeyConstraint("rule_id", name="pk_alert_rules"),
        sa.UniqueConstraint("name", name="uq_alert_rules_name"),
    )
    op.create_index(
        "ix_alert_rules_enabled_metric",
        "alert_rules",
        ["metric_name", "rule_id"],
        unique=False,
        postgresql_where=sa.text("enabled"),
    )

    op.create_table(
        "alert_occurrences",
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observed_value", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolve_reason", sa.String(500), nullable=True),
        sa.CheckConstraint(
            "status IN ('firing', 'acknowledged', 'resolved')",
            name="ck_alert_occurrences_status_value",
        ),
        sa.CheckConstraint(
            _finite_float_check("observed_value"),
            name="ck_alert_occurrences_observed_value_finite",
        ),
        sa.CheckConstraint(
            "sample_count >= 0", name="ck_alert_occurrences_sample_count_nonnegative"
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_alert_occurrences_window"),
        sa.CheckConstraint("version > 0", name="ck_alert_occurrences_version_positive"),
        sa.CheckConstraint(
            "(status = 'firing' AND acknowledged_at IS NULL "
            "AND acknowledged_by IS NULL AND resolved_at IS NULL "
            "AND resolve_reason IS NULL) OR "
            "(status = 'acknowledged' AND acknowledged_at IS NOT NULL "
            "AND acknowledged_by IS NOT NULL AND resolved_at IS NULL "
            "AND resolve_reason IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolve_reason IS NOT NULL)",
            name="ck_alert_occurrences_lifecycle_shape",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.rule_id"],
            name="fk_alert_occurrences_rule_id_alert_rules",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("occurrence_id", name="pk_alert_occurrences"),
    )
    op.create_index(
        "ix_alert_occurrences_rule_fired",
        "alert_occurrences",
        ["rule_id", "fired_at"],
        unique=False,
    )
    op.create_index(
        "uq_alert_occurrences_single_open_per_rule",
        "alert_occurrences",
        ["rule_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('firing', 'acknowledged')"),
    )

    op.create_table(
        "operation_job_receipts",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("applied_targets", sa.JSON(), nullable=False),
        sa.Column("state_versions", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('promote', 'offline', 'restore')",
            name="ck_operation_job_receipts_kind_value",
        ),
        sa.CheckConstraint(
            _sha256_check("input_fingerprint"),
            name="ck_operation_job_receipts_input_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "json_typeof(applied_targets) = 'array'",
            name="ck_operation_job_receipts_targets_array",
        ),
        sa.CheckConstraint(
            "json_typeof(state_versions) = 'object'",
            name="ck_operation_job_receipts_versions_object",
        ),
        sa.CheckConstraint(
            "json_typeof(result) = 'object'",
            name="ck_operation_job_receipts_result_object",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["async_jobs.job_id"],
            name="fk_operation_job_receipts_operation_id_async_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_operation_job_receipts"),
    )
    op.create_index(
        "ix_operation_job_receipts_completed_at",
        "operation_job_receipts",
        ["completed_at", "operation_id"],
        unique=False,
    )

    op.create_table(
        "search_index_builds",
        sa.Column("physical_index", sa.String(128), nullable=False),
        sa.Column("source_version", sa.String(255), nullable=False),
        sa.Column("build_fingerprint", sa.String(64), nullable=False),
        sa.Column("document_count", sa.BigInteger(), nullable=False),
        sa.Column("projection_checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_index", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "physical_index ~ '^microlens-items-[a-z0-9][a-z0-9._-]{0,63}$' "
            "AND physical_index <> 'microlens-items-read'",
            name="ck_search_index_builds_physical_index_namespace",
        ),
        sa.CheckConstraint(
            "source_version <> '' AND source_version <> 'latest'",
            name="ck_search_index_builds_source_version_immutable",
        ),
        sa.CheckConstraint(
            _sha256_check("build_fingerprint"),
            name="ck_search_index_builds_build_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "document_count >= 0", name="ck_search_index_builds_document_count_nonnegative"
        ),
        sa.CheckConstraint(
            _sha256_check("projection_checksum"),
            name="ck_search_index_builds_projection_checksum_sha256",
        ),
        sa.CheckConstraint(
            "status IN ('built', 'active', 'retired')",
            name="ck_search_index_builds_status_value",
        ),
        sa.CheckConstraint(
            "(status = 'active') = (activated_at IS NOT NULL)",
            name="ck_search_index_builds_activation_shape",
        ),
        sa.CheckConstraint(
            "previous_index IS NULL OR (previous_index ~ "
            "'^microlens-items-[a-z0-9][a-z0-9._-]{0,63}$' "
            "AND previous_index <> 'microlens-items-read')",
            name="ck_search_index_builds_previous_index_namespace",
        ),
        sa.PrimaryKeyConstraint("physical_index", name="pk_search_index_builds"),
        sa.UniqueConstraint("build_fingerprint", name="uq_search_index_builds_fingerprint"),
    )
    op.create_index(
        "uq_search_index_builds_single_active",
        "search_index_builds",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_search_index_builds_built_at",
        "search_index_builds",
        ["built_at", "physical_index"],
        unique=False,
    )

    op.create_table(
        "search_index_registry",
        sa.Column("registry_name", sa.String(32), nullable=False),
        sa.Column("read_alias", sa.String(128), nullable=False),
        sa.Column("active_physical_index", sa.String(128), nullable=True),
        sa.Column("last_source_watermark", sa.String(255), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("registry_name = 'items'", name="ck_search_index_registry_name_value"),
        sa.CheckConstraint(
            "read_alias = 'microlens-items-read'",
            name="ck_search_index_registry_alias_value",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_search_index_registry_generation_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["active_physical_index"],
            ["search_index_builds.physical_index"],
            name="fk_search_index_registry_active_index_search_index_builds",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("registry_name", name="pk_search_index_registry"),
        sa.UniqueConstraint("read_alias", name="uq_search_index_registry_read_alias"),
    )

    op.create_table(
        "search_incremental_receipts",
        sa.Column("task_key", sa.String(255), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("physical_index", sa.String(128), nullable=False),
        sa.Column("source_watermark", sa.String(255), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _sha256_check("input_fingerprint"),
            name="ck_search_incremental_receipts_input_fingerprint_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["physical_index"],
            ["search_index_builds.physical_index"],
            name="fk_search_incremental_receipts_index_search_index_builds",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_key", name="pk_search_incremental_receipts"),
    )
    op.create_index(
        "ix_search_incremental_receipts_completed",
        "search_incremental_receipts",
        ["completed_at", "task_key"],
        unique=False,
    )
    op.create_index(
        "ix_search_incremental_receipts_watermark",
        "search_incremental_receipts",
        ["source_watermark", "task_key"],
        unique=False,
    )

    op.create_table(
        "analytics_export_watermarks",
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_manifest_checksum", sa.String(64), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9][a-z0-9._-]{0,254}$'",
            name="ck_analytics_export_watermarks_name_safe",
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0",
            name="ck_analytics_export_watermarks_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "last_manifest_checksum IS NULL OR " + _sha256_check("last_manifest_checksum"),
            name="ck_analytics_export_watermarks_manifest_checksum_sha256",
        ),
        sa.CheckConstraint(
            "version >= 0", name="ck_analytics_export_watermarks_version_nonnegative"
        ),
        sa.PrimaryKeyConstraint("name", name="pk_analytics_export_watermarks"),
    )
    op.execute(
        """
        CREATE FUNCTION microlens_reject_analytics_watermark_regression()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.last_event_sequence < OLD.last_event_sequence THEN
                RAISE EXCEPTION 'analytics watermark cannot regress';
            END IF;
            IF NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'analytics watermark version must advance exactly once';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_analytics_export_watermarks_monotonic
        BEFORE UPDATE ON analytics_export_watermarks
        FOR EACH ROW
        EXECUTE FUNCTION microlens_reject_analytics_watermark_regression()
        """
    )

    op.create_table(
        "analytics_exports",
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("watermark_name", sa.String(255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_event_sequence_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("cutoff_event_sequence_inclusive", sa.BigInteger(), nullable=False),
        sa.Column("expected_watermark_version", sa.BigInteger(), nullable=False),
        sa.Column("parent_manifest_checksum", sa.String(64), nullable=True),
        sa.Column("manifest_checksum", sa.String(64), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "window_end > window_start", name="ck_analytics_exports_window_nonempty"
        ),
        sa.CheckConstraint(
            "previous_event_sequence_exclusive >= 0 AND "
            "cutoff_event_sequence_inclusive >= previous_event_sequence_exclusive",
            name="ck_analytics_exports_watermark_range_monotonic",
        ),
        sa.CheckConstraint(
            "expected_watermark_version >= 0",
            name="ck_analytics_exports_expected_version_nonnegative",
        ),
        sa.CheckConstraint(
            "parent_manifest_checksum IS NULL OR " + _sha256_check("parent_manifest_checksum"),
            name="ck_analytics_exports_parent_manifest_checksum_sha256",
        ),
        sa.CheckConstraint(
            "manifest_checksum IS NULL OR " + _sha256_check("manifest_checksum"),
            name="ck_analytics_exports_manifest_checksum_sha256",
        ),
        sa.CheckConstraint("length(output_path) > 0", name="ck_analytics_exports_path_nonempty"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_analytics_exports_status_value",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (completed_at IS NOT NULL)",
            name="ck_analytics_exports_completion_shape",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND manifest_checksum IS NOT NULL AND error IS NULL) OR "
            "(status = 'failed' AND manifest_checksum IS NULL AND error IS NOT NULL) OR "
            "(status IN ('queued', 'running') AND manifest_checksum IS NULL AND error IS NULL)",
            name="ck_analytics_exports_result_shape",
        ),
        sa.ForeignKeyConstraint(
            ["watermark_name"],
            ["analytics_export_watermarks.name"],
            name="fk_analytics_exports_watermark_name_analytics_export_watermarks",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("export_id", name="pk_analytics_exports"),
        sa.UniqueConstraint(
            "watermark_name",
            "previous_event_sequence_exclusive",
            "cutoff_event_sequence_inclusive",
            name="uq_analytics_exports_watermark_range",
        ),
    )
    op.create_index(
        "ix_analytics_exports_status_created",
        "analytics_exports",
        ["status", "created_at", "export_id"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_exports_window",
        "analytics_exports",
        ["window_start", "window_end", "export_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_exports_window", table_name="analytics_exports")
    op.drop_index("ix_analytics_exports_status_created", table_name="analytics_exports")
    op.drop_table("analytics_exports")
    op.execute(
        "DROP TRIGGER trg_analytics_export_watermarks_monotonic ON analytics_export_watermarks"
    )
    op.execute("DROP FUNCTION microlens_reject_analytics_watermark_regression()")
    op.drop_table("analytics_export_watermarks")

    op.drop_index(
        "ix_search_incremental_receipts_watermark",
        table_name="search_incremental_receipts",
    )
    op.drop_index(
        "ix_search_incremental_receipts_completed",
        table_name="search_incremental_receipts",
    )
    op.drop_table("search_incremental_receipts")
    op.drop_table("search_index_registry")
    op.drop_index("ix_search_index_builds_built_at", table_name="search_index_builds")
    op.drop_index("uq_search_index_builds_single_active", table_name="search_index_builds")
    op.drop_table("search_index_builds")

    op.drop_index("ix_operation_job_receipts_completed_at", table_name="operation_job_receipts")
    op.drop_table("operation_job_receipts")

    op.drop_index("uq_alert_occurrences_single_open_per_rule", table_name="alert_occurrences")
    op.drop_index("ix_alert_occurrences_rule_fired", table_name="alert_occurrences")
    op.drop_table("alert_occurrences")
    op.drop_index("ix_alert_rules_enabled_metric", table_name="alert_rules")
    op.drop_table("alert_rules")

    op.drop_index("ix_async_outbox_delivering_lease", table_name="async_outbox")
    op.drop_index("ix_async_outbox_pending_due", table_name="async_outbox")
    op.drop_table("async_outbox")
    op.drop_index("ix_async_job_attempts_job_id", table_name="async_job_attempts")
    op.drop_index("ix_async_job_attempts_running_lease", table_name="async_job_attempts")
    op.drop_table("async_job_attempts")
    op.drop_index("ix_async_jobs_queued_due", table_name="async_jobs")
    op.drop_table("async_jobs")
