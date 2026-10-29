"""Operator commands. `uv run python -m api.admin create-tenant <name>` creates a Tenant
and prints its API key once; only the key's hash is stored."""

import argparse

from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from core.domain.parties import api_key_prefix, hash_api_key, new_api_key, new_id
from core.ports import Clock, TenantDirectory, TenantId
from core.settings import get_settings


def create_tenant(
    directory: TenantDirectory, name: str, *, live: bool, clock: Clock
) -> tuple[TenantId, str]:
    """Create a Tenant with one API key. Returns the id and the key, which is never
    retrievable again."""
    tenant_id = TenantId(new_id("ten"))
    key = new_api_key(live=live)
    directory.create_tenant(tenant_id, name, api_key_prefix(key), hash_api_key(key), clock.now())
    return tenant_id, key


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m api.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-tenant", help="create a Tenant and print its API key")
    create.add_argument("name")
    args = parser.parse_args(argv)

    settings = get_settings()
    store = PostgresStore(create_engine(str(settings.database_url)))
    tenant_id, key = create_tenant(
        store, args.name, live=settings.env == "production", clock=SystemClock()
    )
    print(f"tenant_id: {tenant_id}")
    print(f"api_key:   {key}")
    print("Store the key now; it is not shown again.")


if __name__ == "__main__":
    main()
