"""Strengthen event identity, audit history and model eligibility invariants.

Revision ID: 20260901_0003
Revises: 20260901_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0003"
down_revision = "20260901_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_exposure_event_identity",
        "exposures",
        ["id", "request_id", "user_id", "item_id", "position"],
    )
    op.drop_constraint("fk_events_exposure_id_exposures", "events", type_="foreignkey")
    op.alter_column("events", "exposure_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_events_exposure_identity",
        "events",
        "exposures",
        ["exposure_id", "request_id", "user_id", "item_id", "position"],
        ["id", "request_id", "user_id", "item_id", "position"],
        ondelete="CASCADE",
    )

    op.create_check_constraint(
        "ck_training_jobs_systems_only_noncomparable_ineligible",
        "training_jobs",
        "purpose <> 'systems_only' OR "
        "(evaluation_comparability = 'non_comparable' AND NOT activation_eligible)",
    )
    op.create_check_constraint(
        "ck_training_jobs_eligible_requires_comparable",
        "training_jobs",
        "NOT activation_eligible OR evaluation_comparability = 'comparable'",
    )

    op.add_column("operation_batches", sa.Column("operator_role", sa.String(32), nullable=True))
    op.execute(
        "UPDATE operation_batches AS batch SET operator_role = users.role "
        "FROM users WHERE users.id = batch.operator_id"
    )
    op.alter_column(
        "operation_batches", "operator_role", existing_type=sa.String(32), nullable=False
    )
    op.create_check_constraint(
        "ck_operation_batches_operator_role",
        "operation_batches",
        "operator_role IN ('user', 'operator_readonly', 'operator', 'admin')",
    )

    op.create_table(
        "model_activation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("expected_current_version", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed')",
            name="ck_model_activation_attempts_status_value",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL AND failure_reason IS NOT NULL) "
            "OR (status <> 'failed' AND failure_code IS NULL AND failure_reason IS NULL)",
            name="ck_model_activation_attempts_failure_shape",
        ),
        sa.ForeignKeyConstraint(
            ["model_version"],
            ["model_versions.model_version"],
            name="fk_model_activation_attempts_model_version_model_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_activation_attempts"),
    )
    op.create_index(
        "ix_model_activation_attempts_model_version",
        "model_activation_attempts",
        ["model_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_activation_attempts_model_version", table_name="model_activation_attempts"
    )
    op.drop_table("model_activation_attempts")

    op.drop_constraint("ck_operation_batches_operator_role", "operation_batches", type_="check")
    op.drop_column("operation_batches", "operator_role")

    op.drop_constraint(
        "ck_training_jobs_eligible_requires_comparable", "training_jobs", type_="check"
    )
    op.drop_constraint(
        "ck_training_jobs_systems_only_noncomparable_ineligible",
        "training_jobs",
        type_="check",
    )

    op.drop_constraint("fk_events_exposure_identity", "events", type_="foreignkey")
    op.alter_column("events", "exposure_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        "fk_events_exposure_id_exposures",
        "events",
        "exposures",
        ["exposure_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_exposure_event_identity", "exposures", type_="unique")
