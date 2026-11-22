"""Demo controls (#280, ADR-0032): the hosted demo's expire-now record, and the
`demo_control` Audit kind.

Revision ID: 0025_demo_controls
Revises: 0024_refunds
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_demo_controls"
down_revision: str | None = "0024_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened', "
    "'dispute_signed_off', 'refund', 'demo_control')"
)
_KINDS_BEFORE = (
    "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened', "
    "'dispute_signed_off', 'refund')"
)


def upgrade() -> None:
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS)
    op.create_table(
        "demo_controls",
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("control_id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_record_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"], ["decisions.tenant_id", "decisions.decision_id"]
        ),
        sa.CheckConstraint(
            "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice')",
            name="demo_controls_kind",
        ),
    )
    op.create_index("ix_demo_controls_target", "demo_controls", ["tenant_id", "kind", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_demo_controls_target", table_name="demo_controls")
    op.drop_table("demo_controls")
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS_BEFORE)
