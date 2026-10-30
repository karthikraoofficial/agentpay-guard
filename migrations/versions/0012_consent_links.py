"""Consent links: hashed tokens for the hosted consent screen (#62, ADR-0008).

Revision ID: 0012_consent_links
Revises: 0011_outbox_events
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_consent_links"
down_revision: str | None = "0011_outbox_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "consent_links",
        sa.Column("token_hash", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("consent_request_id", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "consent_request_id"],
            ["consent_requests.tenant_id", "consent_requests.consent_request_id"],
        ),
    )
    op.create_index(
        "ix_consent_links_request", "consent_links", ["tenant_id", "consent_request_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_consent_links_request", "consent_links")
    op.drop_table("consent_links")
