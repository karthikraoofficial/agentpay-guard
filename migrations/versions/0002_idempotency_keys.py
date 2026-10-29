"""Stored responses for idempotent writes, per Tenant and key.

Revision ID: 0002_idempotency_keys
Revises: 0001_tenants_payers_agents
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_idempotency_keys"
down_revision: str | None = "0001_tenants_payers_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("request_hash", sa.String, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("body", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
