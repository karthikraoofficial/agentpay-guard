"""Sign-off of an Evidence pack (#108): who signed which version, and its pack hash.

Revision ID: 0021_dispute_sign_off
Revises: 0020_disputes
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_dispute_sign_off"
down_revision: str | None = "0020_disputes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened', "
    "'dispute_signed_off')"
)
_KINDS_BEFORE = "kind IN ('decision', 'consent_resolution', 'void', 'payment', 'dispute_opened')"
_COLUMNS = (
    ("reviewer_id", sa.String()),
    ("reviewer_name", sa.String()),
    ("signed_off_version", sa.Integer()),
    ("signed_off_pack_hash", sa.String()),
    ("signed_off_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS)
    for name, kind in _COLUMNS:
        op.add_column("disputes", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("disputes", name)
    op.drop_constraint("audit_records_kind", "audit_records", type_="check")
    op.create_check_constraint("audit_records_kind", "audit_records", _KINDS_BEFORE)
