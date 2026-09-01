"""Add Phase 2B lookup and invariant indexes.

Revision ID: 20260901_0002
Revises: 20260901_0001
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0002"
down_revision = "20260901_0001"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_auth_sessions_user_id", "auth_sessions", ["user_id"]),
    ("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"]),
    ("ix_recommendation_snapshots_user_id", "recommendation_snapshots", ["user_id"]),
    ("ix_recommendation_snapshots_created_at", "recommendation_snapshots", ["created_at"]),
    ("ix_recommendation_requests_snapshot_id", "recommendation_requests", ["snapshot_id"]),
    ("ix_recommendation_requests_user_id", "recommendation_requests", ["user_id"]),
    ("ix_recommendation_requests_created_at", "recommendation_requests", ["created_at"]),
    ("ix_exposures_user_id", "exposures", ["user_id"]),
    ("ix_exposures_item_id", "exposures", ["item_id"]),
    ("ix_exposures_exposed_at", "exposures", ["exposed_at"]),
    ("ix_events_user_id", "events", ["user_id"]),
    ("ix_events_item_id", "events", ["item_id"]),
    ("ix_events_server_timestamp", "events", ["server_timestamp"]),
    ("ix_events_request_item", "events", ["request_id", "item_id"]),
    ("ix_promotion_rules_item_id", "promotion_rules", ["item_id"]),
    ("ix_promotion_rules_operation_batch_id", "promotion_rules", ["operation_batch_id"]),
    ("ix_operations_batch_id", "operations", ["batch_id"]),
    ("ix_job_attempts_job_id", "job_attempts", ["job_id"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, unique=False)
    op.execute(
        "CREATE UNIQUE INDEX uq_events_canonical_impression_exposure "
        "ON events (exposure_id) WHERE event_type = 'impression'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_versions_single_active "
        "ON model_versions ((1)) WHERE status = 'ACTIVE'"
    )


def downgrade() -> None:
    op.drop_index("uq_model_versions_single_active", table_name="model_versions")
    op.drop_index("uq_events_canonical_impression_exposure", table_name="events")
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
