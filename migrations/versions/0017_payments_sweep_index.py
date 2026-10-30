"""An index for the reconciliation sweep's lookup of scheduled Payments (#82).

Revision ID: 0017_payments_sweep_index
Revises: 0016_rail_events
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_payments_sweep_index"
down_revision: str | None = "0016_rail_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_payments_status_created", table_name="payments")
