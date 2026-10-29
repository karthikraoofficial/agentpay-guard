"""Mandates. Terms are never updated; only revoked_by and revoked_at are set, once.

Revision ID: 0003_mandates
Revises: 0002_idempotency_keys
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0003_mandates"
down_revision: str | None = "0002_idempotency_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "mandates",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("mandate_id", sa.String, primary_key=True),
        sa.Column("payer_id", sa.String, nullable=False),
        sa.Column("allowed_agent_ids", ARRAY(sa.String), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("per_txn_cap_minor", sa.BigInteger, nullable=False),
        sa.Column("monthly_cap_minor", sa.BigInteger, nullable=False),
        sa.Column("allowed_mccs", ARRAY(sa.String), nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("supersedes", sa.String, nullable=True),
        sa.Column("consent_method", sa.String, nullable=False),
        sa.Column("consent_reference", sa.String, nullable=False),
        sa.Column("consent_captured_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_by", sa.String, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "payer_id"], ["payers.tenant_id", "payers.payer_id"]
        ),
        sa.CheckConstraint("revoked_by IN ('payer', 'tenant')", name="mandates_revoked_by_valid"),
        sa.CheckConstraint(
            "(revoked_by IS NULL) = (revoked_at IS NULL)", name="mandates_revocation_whole"
        ),
    )
    op.create_index(
        "ix_mandates_tenant_payer_created",
        "mandates",
        ["tenant_id", "payer_id", "created_at", "mandate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_mandates_tenant_payer_created", "mandates")
    op.drop_table("mandates")
