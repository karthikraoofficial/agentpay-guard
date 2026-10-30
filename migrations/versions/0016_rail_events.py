"""Rail events: the inbound rail webhooks acted on, for deduplication (#81).

Revision ID: 0016_rail_events
Revises: 0015_voids
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_rail_events"
down_revision: str | None = "0015_voids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rail_events",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("event_id", sa.String, primary_key=True),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("payment_id", sa.String, nullable=False),
        sa.Column("rail_payment_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("failure_code", sa.String, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "payment_id"], ["payments.tenant_id", "payments.payment_id"]
        ),
        sa.CheckConstraint("status IN ('captured', 'failed')", name="rail_events_status"),
    )


def downgrade() -> None:
    op.drop_table("rail_events")
