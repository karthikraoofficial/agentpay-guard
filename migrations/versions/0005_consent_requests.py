"""Pending Consent requests for step_up Decisions. Expiry is computed when read.

Revision ID: 0005_consent_requests
Revises: 0004_decisions_audit
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_consent_requests"
down_revision: str | None = "0004_decisions_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "consent_requests",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("consent_request_id", sa.String, primary_key=True),
        sa.Column("decision_id", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"], ["decisions.tenant_id", "decisions.decision_id"]
        ),
    )
    op.create_index(
        "ux_consent_requests_decision",
        "consent_requests",
        ["tenant_id", "decision_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_consent_requests_decision", "consent_requests")
    op.drop_table("consent_requests")
