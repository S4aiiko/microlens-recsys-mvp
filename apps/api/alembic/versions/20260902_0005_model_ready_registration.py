"""store exact model data-manifest lineage

Revision ID: 20260902_0005
Revises: 20260902_0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0005"
down_revision: str | None = "20260902_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing Phase 2 protocol rows did not persist this lineage field. Keep them
    # readable while requiring every new READY registration to populate it.
    op.add_column(
        "model_versions",
        sa.Column("data_manifest_checksum", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_model_versions_data_manifest_checksum_length",
        "model_versions",
        "data_manifest_checksum IS NULL OR length(data_manifest_checksum) = 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_versions_data_manifest_checksum_length",
        "model_versions",
        type_="check",
    )
    op.drop_column("model_versions", "data_manifest_checksum")
