"""Decisions record their merchant, for the New merchant Signal (#39).

Existing rows are backfilled from their Audit record's request, which holds the merchant
every Decision was made for, so history from before this migration still counts.

Revision ID: 0007_decision_merchant
Revises: 0006_intent_checks
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_decision_merchant"
down_revision: str | None = "0006_intent_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("merchant_id", sa.String, nullable=True))
    op.execute(
        """
        UPDATE decisions AS d
        SET merchant_id = convert_from(a.body, 'UTF8')::jsonb
            -> 'inputs' -> 'request' -> 'merchant' ->> 'id'
        FROM audit_records AS a
        WHERE a.tenant_id = d.tenant_id AND a.audit_record_id = d.audit_record_id
        """
    )
    op.alter_column("decisions", "merchant_id", nullable=False)
    op.create_index(
        "ix_decisions_tenant_merchant_outcome",
        "decisions",
        ["tenant_id", "merchant_id", "outcome"],
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_tenant_merchant_outcome", "decisions")
    op.drop_column("decisions", "merchant_id")
