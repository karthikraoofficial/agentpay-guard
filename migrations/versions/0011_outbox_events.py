"""The transactional outbox (#60, ADR-0009).

A step_up Decision writes a `consent_requested` event here in its own transaction; the
relay starts the consent workflow and sets `delivered_at`.

Revision ID: 0011_outbox_events
Revises: 0010_resolution_reason_codes
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_outbox_events"
down_revision: str | None = "0010_resolution_reason_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("event_id", sa.String, primary_key=True),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("delivered_at", TS, nullable=True),
    )
    op.create_index(
        "ix_outbox_events_undelivered",
        "outbox_events",
        ["occurred_at", "event_id"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_undelivered", "outbox_events")
    op.drop_table("outbox_events")
