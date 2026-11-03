"""Review links (#111): expiring links to a Dispute's Evidence pack for a named reviewer.

Revision ID: 0022_review_links
Revises: 0021_dispute_sign_off
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_review_links"
down_revision: str | None = "0021_dispute_sign_off"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_links",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("dispute_id", sa.String(), nullable=False),
        sa.Column("reviewer_id", sa.String(), nullable=False),
        sa.Column("reviewer_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "dispute_id"], ["disputes.tenant_id", "disputes.dispute_id"]
        ),
    )
    op.create_index("ix_review_links_dispute", "review_links", ["tenant_id", "dispute_id"])


def downgrade() -> None:
    op.drop_index("ix_review_links_dispute", table_name="review_links")
    op.drop_table("review_links")
