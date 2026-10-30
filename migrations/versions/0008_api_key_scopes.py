"""API keys carry scopes (ADR-0011), and Consent requests can be listed newest first (#57).

Keys created before scopes existed get every scope, so no integration breaks. The default
is dropped afterwards, so every new key must say what it may do.

Revision ID: 0008_api_key_scopes
Revises: 0007_decision_merchant
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0008_api_key_scopes"
down_revision: str | None = "0007_decision_merchant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            ARRAY(sa.String),
            nullable=False,
            server_default=sa.text("'{consent,decide,manage}'"),
        ),
    )
    op.alter_column("api_keys", "scopes", server_default=None)
    op.create_index(
        "ix_consent_requests_tenant_created",
        "consent_requests",
        ["tenant_id", "created_at", "consent_request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_consent_requests_tenant_created", "consent_requests")
    op.drop_column("api_keys", "scopes")
