"""Table definitions shared by the Postgres store and Alembic. Every table has
tenant_id (non-negotiable 3); tenant-owned tables key on (tenant_id, id)."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
)

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
