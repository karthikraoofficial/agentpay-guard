"""Voids: an allow or approved step-up released from Spent this month (#80).

Revision ID: 0015_voids
Revises: 0014_payments
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_voids"
down_revision: str | None = "0014_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voids",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("decision_id", sa.String, primary_key=True),
        sa.Column("via", sa.String, nullable=False),
        sa.Column("payment_id", sa.String, nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_record_id", sa.String, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"], ["decisions.tenant_id", "decisions.decision_id"]
        ),
        sa.CheckConstraint("via IN ('tenant_api', 'payment_failed')", name="voids_via"),
    )


def downgrade() -> None:
    op.drop_table("voids")
