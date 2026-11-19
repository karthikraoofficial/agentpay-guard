"""Refunds (#206): money returned on a captured Payment, and the `refund` Audit kind.

Revision ID: 0024_refunds
Revises: 0023_model_calls
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_refunds"
down_revision: str | None = "0023_model_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened', "
    "'dispute_signed_off', 'refund')"
)
_KINDS_BEFORE = (
    "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened', "
    "'dispute_signed_off')"
)


def upgrade() -> None:
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS)
    op.create_table(
        "refunds",
        sa.Column(
            "tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), primary_key=True
        ),
        sa.Column("refund_id", sa.String(), primary_key=True),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("mandate_id", sa.String(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rail_refund_id", sa.String(), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("pack_id", sa.String(), nullable=False),
        sa.Column("pack_version", sa.String(), nullable=False),
        sa.Column("pack_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "payment_id"], ["payments.tenant_id", "payments.payment_id"]
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processed', 'failed')", name="refunds_status"
        ),
        sa.CheckConstraint("amount_minor > 0", name="refunds_amount_positive"),
    )
    op.create_index("ix_refunds_tenant_payment", "refunds", ["tenant_id", "payment_id"])


def downgrade() -> None:
    op.drop_index("ix_refunds_tenant_payment", table_name="refunds")
    op.drop_table("refunds")
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS_BEFORE)
