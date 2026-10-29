"""Empty baseline. Phase 1 adds the first tables, each with tenant_id.

Revision ID: 0000_baseline
Revises:
Create Date: 2026-10-29
"""

from collections.abc import Sequence

revision: str = "0000_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
