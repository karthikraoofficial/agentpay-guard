"""Disputes and Evidence packs (#105): the `dispute_opened` Audit kind, and a lookup of a
Decision's Audit records.

Revision ID: 0020_disputes
Revises: 0019_tenant_webhooks
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_disputes"
down_revision: str | None = "0019_tenant_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened')"
_KINDS_BEFORE = "kind IN ('decision', 'consent_resolution', 'void', 'payment')"


def upgrade() -> None:
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS)
    op.create_table(
        "disputes",
        sa.Column(
            "tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), primary_key=True
        ),
        sa.Column("dispute_id", sa.String(), primary_key=True),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("respond_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payer_statement", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("opened_seq", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "payment_id"], ["payments.tenant_id", "payments.payment_id"]
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_review', 'ready', 'expired', 'withdrawn')",
            name="disputes_status",
        ),
    )
    op.create_index(
        "ux_disputes_tenant_payment", "disputes", ["tenant_id", "payment_id"], unique=True
    )
    op.create_table(
        "evidence_packs",
        sa.Column(
            "tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), primary_key=True
        ),
        sa.Column("dispute_id", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("bundle_hash", sa.String(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dispute_id"], ["disputes.tenant_id", "disputes.dispute_id"]
        ),
    )
    op.create_index(
        "ix_audit_records_tenant_decision", "audit_records", ["tenant_id", "decision_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_records_tenant_decision", table_name="audit_records")
    op.drop_table("evidence_packs")
    op.drop_index("ux_disputes_tenant_payment", table_name="disputes")
    op.drop_table("disputes")
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS_BEFORE)
