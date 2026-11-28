"""The settle-now Demo control (#287, ADR-0032): one more `demo_controls` kind. Existing
rows are untouched.

Downgrading fails while any `settle_now` control exists, since the narrower check would
reject it.

Revision ID: 0029_demo_control_settle_now
Revises: 0028_demo_control_refund_window
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_demo_control_settle_now"
down_revision: str | None = "0028_demo_control_refund_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice', "
    "'expire_dispute', 'expire_refund_window', 'settle_now', 'ai_off', "
    "'rail_declining', 'lose_payment', 'rail_down')"
)
_KINDS_BEFORE = (
    "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice', "
    "'expire_dispute', 'expire_refund_window', 'ai_off', 'rail_declining', "
    "'lose_payment', 'rail_down')"
)


def upgrade() -> None:
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS)


def downgrade() -> None:
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS_BEFORE)
