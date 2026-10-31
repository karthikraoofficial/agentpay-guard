"""Tenant webhooks: one endpoint per Tenant with its encrypted signing secret (ADR-0015),
and retry state on outbox events.

Revision ID: 0019_tenant_webhooks
Revises: 0018_payments_debit_after
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_tenant_webhooks"
down_revision: str | None = "0018_payments_debit_after"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column(
            "tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), primary_key=True
        ),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret_ciphertext", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "outbox_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "outbox_events",
        sa.Column("gave_up", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "gave_up")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "attempts")
    op.drop_table("webhook_endpoints")
