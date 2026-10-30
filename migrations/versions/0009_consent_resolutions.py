"""Consent resolutions: how each Consent request ended (#58, ADR-0009).

One row per request (the primary key), so the first resolution written wins. The
evidence is the resolution's Audit record; this table is for looking statuses up.

Revision ID: 0009_consent_resolutions
Revises: 0008_api_key_scopes
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_consent_resolutions"
down_revision: str | None = "0008_api_key_scopes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "consent_resolutions",
        sa.Column("tenant_id", sa.String, primary_key=True),
        sa.Column("consent_request_id", sa.String, primary_key=True),
        sa.Column("decision_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("resolved_at", TS, nullable=False),
        sa.Column("resolved_via", sa.String, nullable=False),
        sa.Column("attestation_method", sa.String, nullable=True),
        sa.Column("attestation_reference", sa.String, nullable=True),
        sa.Column("attestation_captured_at", TS, nullable=True),
        sa.Column("audit_record_id", sa.String, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "consent_request_id"],
            ["consent_requests.tenant_id", "consent_requests.consent_request_id"],
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'declined', 'expired', 'blocked')",
            name="ck_consent_resolutions_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("consent_resolutions")
