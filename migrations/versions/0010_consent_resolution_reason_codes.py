"""A blocked approval keeps the re-check's Reason codes (#59, ADR-0010).

Revision ID: 0010_resolution_reason_codes
Revises: 0009_consent_resolutions
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0010_resolution_reason_codes"
down_revision: str | None = "0009_consent_resolutions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "consent_resolutions", sa.Column("reason_codes", ARRAY(sa.String), nullable=True)
    )
    op.create_index(
        "ix_consent_resolutions_status_resolved",
        "consent_resolutions",
        ["tenant_id", "status", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_consent_resolutions_status_resolved", "consent_resolutions")
    op.drop_column("consent_resolutions", "reason_codes")
