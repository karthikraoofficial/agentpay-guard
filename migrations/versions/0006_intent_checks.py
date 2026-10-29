"""Intent checks: a model's verdict on a cart against the Payer instruction (ADR-0007).

Revision ID: 0006_intent_checks
Revises: 0005_consent_requests
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_intent_checks"
down_revision: str | None = "0005_consent_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "intent_checks",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("intent_id", sa.String, primary_key=True),
        sa.Column("mandate_id", sa.String, nullable=False),
        sa.Column("agent_id", sa.String, nullable=False),
        sa.Column("merchant_id", sa.String, nullable=False),
        sa.Column("merchant_mcc", sa.String(4), nullable=False),
        sa.Column("quoted_total_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=True),
        sa.Column("content_hash", sa.String, nullable=False),
        sa.Column("verdict", sa.String, nullable=False),
        sa.Column("failure_cause", sa.String, nullable=True),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("prompt_version", sa.String, nullable=False),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        sa.Column("cost_usd_micros", sa.BigInteger, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "mandate_id"], ["mandates.tenant_id", "mandates.mandate_id"]
        ),
        sa.CheckConstraint(
            "verdict IN ('match', 'mismatch', 'unknown')", name="intent_verdict_valid"
        ),
        sa.CheckConstraint(
            "(failure_cause IS NULL) OR (verdict = 'unknown')", name="intent_cause_means_unknown"
        ),
    )


def downgrade() -> None:
    op.drop_table("intent_checks")
