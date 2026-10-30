"""Payments: one per Decision, scheduled then captured or failed (#79, ADR-0014), and the
`payment` Audit record kind.

Downgrading fails once any `payment` Audit record exists: the Audit chain is append-only,
so those records can't be removed to fit the old kinds.

Revision ID: 0014_payments
Revises: 0013_mandate_rail_token
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_payments"
down_revision: str | None = "0013_mandate_rail_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BEFORE = "kind IN ('decision', 'consent_resolution', 'void')"
_KINDS = "kind IN ('decision', 'consent_resolution', 'void', 'payment')"


def upgrade() -> None:
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS)
    op.create_table(
        "payments",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("payment_id", sa.String, primary_key=True),
        sa.Column("decision_id", sa.String, nullable=False),
        sa.Column("mandate_id", sa.String, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("rail", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("rail_payment_id", sa.String, nullable=True),
        sa.Column("failure_code", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"], ["decisions.tenant_id", "decisions.decision_id"]
        ),
        sa.CheckConstraint("status IN ('scheduled', 'captured', 'failed')", name="payments_status"),
        sa.CheckConstraint("amount_minor > 0", name="payments_amount_positive"),
    )
    op.create_index(
        "ux_payments_tenant_decision", "payments", ["tenant_id", "decision_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_payments_tenant_decision", table_name="payments")
    op.drop_table("payments")
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS_BEFORE)
