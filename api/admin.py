"""Operator commands, run as `uv run python -m api.admin <command>`.

- `create-tenant <name>`: creates a Tenant and prints its API key once; only the hash
  is stored.
- `replay <audit_record_id> --tenant <id>`: re-runs a recorded Decision and checks the
  output is byte-identical. Exit 0 identical, 1 different (a diff is printed),
  2 pack missing or not the recorded one, 3 no such record for that Tenant.
- `verify --tenant <id>`: walks the Tenant's Audit chain. Exit 0 clean, 1 broken (the
  first broken seq is printed).
"""

import argparse
import difflib
import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine

from adapters.postgres.store import PostgresStore
from adapters.system_clock import SystemClock
from core.domain.audit import first_broken_seq
from core.domain.decisions import AuditRecordId
from core.domain.parties import api_key_prefix, hash_api_key, new_api_key, new_id
from core.policy.packs import PackError, load_pack
from core.policy.replay import PackMismatch, replay
from core.ports import Clock, TenantDirectory, TenantId, UnitOfWork
from core.settings import get_settings

Out = Callable[[str], None]

IDENTICAL, DIFFERENT, PACK_PROBLEM, NOT_FOUND = 0, 1, 2, 3


def create_tenant(
    directory: TenantDirectory, name: str, *, live: bool, clock: Clock
) -> tuple[TenantId, str]:
    """Create a Tenant with one API key. Returns the id and the key, which is never
    retrievable again."""
    tenant_id = TenantId(new_id("ten"))
    key = new_api_key(live=live)
    directory.create_tenant(tenant_id, name, api_key_prefix(key), hash_api_key(key), clock.now())
    return tenant_id, key


def replay_record(
    store: UnitOfWork, tenant_id: TenantId, audit_record_id: str, packs_dir: Path, out: Out
) -> int:
    with store.transaction(tenant_id) as tx:
        sealed = tx.get_audit_record(AuditRecordId(audit_record_id))
    if sealed is None:
        out(f"No Audit record {audit_record_id} for Tenant {tenant_id}.")
        return NOT_FOUND
    recorded = sealed.record.pack
    try:
        loaded = load_pack(packs_dir, recorded.id, recorded.version)
        result = replay(sealed.body, loaded.pack, loaded.hash)
    except (PackError, PackMismatch) as exc:
        out(f"Cannot replay with the recorded Policy pack: {exc}")
        return PACK_PROBLEM
    if result.identical:
        out(f"Identical: {audit_record_id} (seq {sealed.seq}) replays byte for byte.")
        return IDENTICAL
    out(f"Different: {audit_record_id} (seq {sealed.seq}) does not replay identically.")
    out(
        "".join(
            difflib.unified_diff(
                _lines(result.recorded), _lines(result.replayed), "recorded", "replayed"
            )
        )
    )
    return DIFFERENT


def verify_chain(store: UnitOfWork, tenant_id: TenantId, out: Out) -> int:
    with store.transaction(tenant_id) as tx:
        rows, head = tx.read_audit_chain()
    broken = first_broken_seq(rows, head)
    if broken is None:
        out(f"Clean: {len(rows)} Audit records for Tenant {tenant_id} chain correctly.")
        return IDENTICAL
    out(f"Broken: the Audit chain for Tenant {tenant_id} first fails at seq {broken}.")
    return DIFFERENT


def _lines(canonical: bytes) -> list[str]:
    return (json.dumps(json.loads(canonical), indent=2, sort_keys=True) + "\n").splitlines(True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-tenant", help="create a Tenant and print its API key")
    create.add_argument("name")
    rep = commands.add_parser("replay", help="re-run a recorded Decision and compare bytes")
    rep.add_argument("audit_record_id")
    rep.add_argument("--tenant", required=True)
    ver = commands.add_parser("verify", help="check a Tenant's Audit chain")
    ver.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    store = PostgresStore(create_engine(str(settings.database_url)))
    if args.command == "replay":
        return replay_record(
            store, TenantId(args.tenant), args.audit_record_id, settings.policy_packs_dir, print
        )
    if args.command == "verify":
        return verify_chain(store, TenantId(args.tenant), print)
    tenant_id, key = create_tenant(
        store, args.name, live=settings.env == "production", clock=SystemClock()
    )
    print(f"tenant_id: {tenant_id}")
    print(f"api_key:   {key}")
    print("Store the key now; it is not shown again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
