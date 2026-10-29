"""Decisions, the append-only Audit records, and each Tenant's Audit chain head.

Triggers reject UPDATE, DELETE and TRUNCATE on audit_records for every role, including
the table owner, so the chain can only grow (ADR-0004).

Revision ID: 0004_decisions_audit
Revises: 0003_mandates
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0004_decisions_audit"
down_revision: str | None = "0003_mandates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "decisions",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("decision_id", sa.String, primary_key=True),
        sa.Column("mandate_id", sa.String, nullable=False),
        sa.Column("agent_id", sa.String, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("outcome", sa.String, nullable=False),
        sa.Column("reason_codes", ARRAY(sa.String), nullable=False),
        sa.Column("risk_tier", sa.String, nullable=False),
        sa.Column("pack_id", sa.String, nullable=False),
        sa.Column("pack_version", sa.String, nullable=False),
        sa.Column("pack_hash", sa.String, nullable=False),
        sa.Column("audit_record_id", sa.String, nullable=False),
        sa.Column("decided_at", TS, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "mandate_id"], ["mandates.tenant_id", "mandates.mandate_id"]
        ),
        sa.CheckConstraint("outcome IN ('allow', 'step_up', 'block')", name="decisions_outcome"),
    )
    op.create_index(
        "ix_decisions_tenant_mandate_decided",
        "decisions",
        ["tenant_id", "mandate_id", "decided_at"],
    )
    op.create_table(
        "audit_records",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("audit_record_id", sa.String, nullable=False, unique=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("decision_id", sa.String, nullable=False),
        sa.Column("body", sa.LargeBinary, nullable=False),
        sa.Column("prev_hash", sa.String, nullable=False),
        sa.Column("record_hash", sa.String, nullable=False),
        sa.CheckConstraint("seq >= 1", name="audit_records_seq_positive"),
        sa.CheckConstraint(
            "kind IN ('decision', 'consent_resolution', 'void')", name="audit_records_kind"
        ),
    )
    op.create_table(
        "tenant_chain_heads",
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.tenant_id"), primary_key=True),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("record_hash", sa.String, nullable=False),
    )
    op.execute(
        """
        CREATE FUNCTION audit_records_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_records is append-only: % is not allowed', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_no_update_or_delete
        BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION audit_records_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_no_truncate
        BEFORE TRUNCATE ON audit_records
        FOR EACH STATEMENT EXECUTE FUNCTION audit_records_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_records_no_truncate ON audit_records")
    op.execute("DROP TRIGGER audit_records_no_update_or_delete ON audit_records")
    op.execute("DROP FUNCTION audit_records_append_only()")
    op.drop_table("tenant_chain_heads")
    op.drop_table("audit_records")
    op.drop_index("ix_decisions_tenant_mandate_decided", "decisions")
    op.drop_table("decisions")
