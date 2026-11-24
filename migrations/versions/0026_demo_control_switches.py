"""Demo control switches (#281, ADR-0032): a switch runs before any Decision, so only a
`demo_control` Audit record may have no `decision_id`. Existing rows and their hashes are
untouched.

Downgrading fails while any switch record exists, because the Audit chain is append-only
and those records can't be given a Decision.

Revision ID: 0026_demo_control_switches
Revises: 0025_demo_controls
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_demo_control_switches"
down_revision: str | None = "0025_demo_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice', 'ai_off', "
    "'rail_declining', 'lose_payment', 'rail_down')"
)
_KINDS_BEFORE = "kind IN ('expire_consent', 'expire_payment_window', 'expire_notice')"
_ONLY_CONTROLS = "decision_id IS NOT NULL OR kind = 'demo_control'"


def upgrade() -> None:
    op.alter_column("audit_records", "decision_id", existing_type=sa.String(), nullable=True)
    op.create_check_constraint("audit_records_decision_id", "audit_records", _ONLY_CONTROLS)
    op.alter_column("demo_controls", "decision_id", existing_type=sa.String(), nullable=True)
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS)


def downgrade() -> None:
    op.drop_constraint("demo_controls_kind", "demo_controls", type_="check")
    op.create_check_constraint("demo_controls_kind", "demo_controls", _KINDS_BEFORE)
    op.alter_column("demo_controls", "decision_id", existing_type=sa.String(), nullable=False)
    op.drop_constraint("audit_records_decision_id", "audit_records", type_="check")
    op.alter_column("audit_records", "decision_id", existing_type=sa.String(), nullable=False)
