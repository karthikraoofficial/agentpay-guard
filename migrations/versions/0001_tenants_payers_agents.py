"""Tenants, API keys, Payers and Agents.

Revision ID: 0001_tenants_payers_agents
Revises: 0000_baseline
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_tenants_payers_agents"
down_revision: str | None = "0000_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("key_hash", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), nullable=False),
        sa.Column("key_prefix", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "payers",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("payer_id", sa.String, primary_key=True),
        sa.Column("display_name", sa.String, nullable=False),
        sa.Column("contact_ref", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "agents",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("agent_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="agents_status_valid"),
    )


def downgrade() -> None:
    op.drop_table("agents")
    op.drop_table("payers")
    op.drop_index("ix_api_keys_tenant_id", "api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")
