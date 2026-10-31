"""When a scheduled Payment's card may be debited, and the debit step's index (ADR-0017).

Revision ID: 0018_payments_debit_after
Revises: 0017_payments_sweep_index
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_payments_debit_after"
down_revision: str | None = "0017_payments_sweep_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("debit_after", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_payments_status_debit_after", "payments", ["status", "debit_after"])


def downgrade() -> None:
    op.drop_index("ix_payments_status_debit_after", table_name="payments")
    op.drop_column("payments", "debit_after")
