"""Model spend cap (#134): model calls counted per Tenant, purpose and UTC day.

Revision ID: 0023_model_calls
Revises: 0022_review_links
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_model_calls"
down_revision: str | None = "0022_review_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "purpose", "day"),
        sa.CheckConstraint("calls >= 1", name="model_calls_counted"),
    )


def downgrade() -> None:
    op.drop_table("model_calls")
