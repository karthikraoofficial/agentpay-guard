"""Rail token on Mandates: the instrument Payments charge (#77, ADR-0013).

Revision ID: 0013_mandate_rail_token
Revises: 0012_consent_links
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_mandate_rail_token"
down_revision: str | None = "0012_consent_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("rail", "rail_customer_ref", "rail_token_ref", "rail_display")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("mandates", sa.Column(name, sa.String, nullable=True))
    op.create_check_constraint(
        "mandates_rail_token_whole",
        "mandates",
        "(rail IS NULL) = (rail_customer_ref IS NULL) AND (rail IS NULL) = (rail_token_ref IS NULL)"
        " AND (rail IS NULL) = (rail_display IS NULL) AND rail IN ('razorpay', 'mock')",
    )


def downgrade() -> None:
    op.drop_constraint("mandates_rail_token_whole", "mandates", type_="check")
    for name in reversed(_COLUMNS):
        op.drop_column("mandates", name)
