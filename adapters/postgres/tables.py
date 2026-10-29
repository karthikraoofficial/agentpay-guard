"""Table definitions shared by the Postgres store and Alembic. Every table has
tenant_id (non-negotiable 3); tenant-owned tables key on (tenant_id, id)."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import ARRAY

metadata = MetaData()

tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("key_hash", String, primary_key=True),
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), nullable=False, index=True),
    Column("key_prefix", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

payers = Table(
    "payers",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("payer_id", String, primary_key=True),
    Column("display_name", String, nullable=False),
    Column("contact_ref", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

agents = Table(
    "agents",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("agent_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('active', 'disabled')", name="agents_status_valid"),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("key", String, primary_key=True),
    Column("request_hash", String, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("body", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

mandates = Table(
    "mandates",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("mandate_id", String, primary_key=True),
    Column("payer_id", String, nullable=False),
    Column("allowed_agent_ids", ARRAY(String), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("per_txn_cap_minor", BigInteger, nullable=False),
    Column("monthly_cap_minor", BigInteger, nullable=False),
    Column("allowed_mccs", ARRAY(String), nullable=False),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("supersedes", String, nullable=True),
    Column("consent_method", String, nullable=False),
    Column("consent_reference", String, nullable=False),
    Column("consent_captured_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_by", String, nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["tenant_id", "payer_id"], ["payers.tenant_id", "payers.payer_id"]),
    CheckConstraint("revoked_by IN ('payer', 'tenant')", name="mandates_revoked_by_valid"),
    CheckConstraint(
        "(revoked_by IS NULL) = (revoked_at IS NULL)", name="mandates_revocation_whole"
    ),
    Index("ix_mandates_tenant_payer_created", "tenant_id", "payer_id", "created_at", "mandate_id"),
)

decisions = Table(
    "decisions",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("decision_id", String, primary_key=True),
    Column("mandate_id", String, nullable=False),
    Column("agent_id", String, nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("outcome", String, nullable=False),
    Column("reason_codes", ARRAY(String), nullable=False),
    Column("risk_tier", String, nullable=False),
    Column("pack_id", String, nullable=False),
    Column("pack_version", String, nullable=False),
    Column("pack_hash", String, nullable=False),
    Column("audit_record_id", String, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "mandate_id"], ["mandates.tenant_id", "mandates.mandate_id"]
    ),
    Index("ix_decisions_tenant_mandate_decided", "tenant_id", "mandate_id", "decided_at"),
)

audit_records = Table(
    "audit_records",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    Column("audit_record_id", String, nullable=False, unique=True),
    Column("kind", String, nullable=False),
    Column("decision_id", String, nullable=False),
    Column("body", LargeBinary, nullable=False),
    Column("prev_hash", String, nullable=False),
    Column("record_hash", String, nullable=False),
)

tenant_chain_heads = Table(
    "tenant_chain_heads",
    metadata,
    Column("tenant_id", String, ForeignKey("tenants.tenant_id"), primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("record_hash", String, nullable=False),
)
