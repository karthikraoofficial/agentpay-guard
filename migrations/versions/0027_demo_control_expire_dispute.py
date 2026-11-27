"""The expire-now Demo control for a Dispute's deadline (#284, ADR-0032): one more
`demo_controls` kind. Existing rows are untouched.

Downgrading fails while any `expire_dispute` control exists, since the narrower check
would reject it.

Revision ID: 0027_demo_control_expire_dispute
Revises: 0026_demo_control_switches
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_demo_control_expire_dispute"
down_revision: str | None = "0026_demo_control_switches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice', "
    "'expire_dispute', 'ai_off', 'rail_declining', 'lose_payment', 'rail_down')"
)
_KINDS_BEFORE = (
    "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice', 'ai_off', "
    "'rail_declining', 'lose_payment', 'rail_down')"
)


def upgrade() -> None:
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS)


def downgrade() -> None:
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS_BEFORE)
