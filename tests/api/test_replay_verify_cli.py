"""The replay and verify operator commands, against Postgres."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from adapters.postgres.store import PostgresStore
from api import admin
from api.main import create_app
from core.policy.packs import hash_file
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_decisions import Pay
from tests.conftest import make_settings

PACKS = Path(__file__).resolve().parents[2] / "policy_packs"
ACTIVE_FILE = "1.2.0.yaml"  # the pack new Decisions are made with


@pytest.fixture
def engine(migrated_database_url: str) -> Engine:
    return create_engine(migrated_database_url)


@pytest.fixture
def pay(engine: Engine) -> Pay:
    store = PostgresStore(engine)
    clock = MovableClock(NOW)
    return Pay(Api(TestClient(create_app(make_settings(), store=store, clock=clock)), store, clock))


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    *argv: str,
    packs: Path = PACKS,
) -> tuple[int, str]:
    monkeypatch.setattr(
        admin, "get_settings", lambda: make_settings(database_url=url, policy_packs_dir=packs)
    )
    code = admin.main(list(argv))
    return code, capsys.readouterr().out


def tamper(engine: Engine, sql: str, **params: object) -> None:
    """Edit the table as a superuser would, with triggers off for this session only."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(sql), params)


@pytest.fixture
def cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrated_database_url: str
):  # type: ignore[no-untyped-def]
    def call(*argv: str, packs: Path = PACKS) -> tuple[int, str]:
        return run(monkeypatch, capsys, migrated_database_url, *argv, packs=packs)

    return call


# --- replay ---------------------------------------------------------------------------


def test_a_recorded_decision_replays_identically(pay: Pay, cli) -> None:  # type: ignore[no-untyped-def]
    audit_id = pay.ask().json()["audit_record_id"]

    code, out = cli("replay", audit_id, "--tenant", pay.tenant_id())

    assert code == 0
    assert "Identical" in out


def test_a_decision_on_a_mandate_with_a_rail_token_replays_and_verifies(pay: Pay, cli) -> None:  # type: ignore[no-untyped-def]
    token = {
        "rail": "razorpay",
        "customer_ref": "cust_A1",
        "token_ref": "token_A1",
        "display": "visa •••• 1111",
    }
    mandate_id = pay.create(rail_token=token)["mandate_id"]
    audit_id = pay.ask(mandate_id=mandate_id).json()["audit_record_id"]

    replayed = cli("replay", audit_id, "--tenant", pay.tenant_id())
    verified = cli("verify", "--tenant", pay.tenant_id())

    assert replayed[0] == 0 and "Identical" in replayed[1]
    assert verified[0] == 0


def test_replay_only_reads_the_given_tenants_records(pay: Pay, cli) -> None:  # type: ignore[no-untyped-def]
    audit_id = pay.ask().json()["audit_record_id"]
    other = Pay(pay.api, "other")

    code, out = cli("replay", audit_id, "--tenant", other.tenant_id())

    assert code == 3
    assert "No Audit record" in out


def test_replay_with_a_pack_whose_hash_differs_is_a_pack_error_not_a_diff(
    pay: Pay,
    cli,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    audit_id = pay.ask().json()["audit_record_id"]
    shutil.copytree(PACKS, tmp_path, dirs_exist_ok=True)
    pack_file = tmp_path / "default" / ACTIVE_FILE
    pack_file.write_text(
        pack_file.read_text(encoding="utf-8").replace("500000", "400000"), encoding="utf-8"
    )
    manifest = tmp_path / "manifest.json"
    original = hash_file(PACKS / "default" / ACTIVE_FILE)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(original, hash_file(pack_file)),
        encoding="utf-8",
    )

    code, out = cli("replay", audit_id, "--tenant", pay.tenant_id(), packs=tmp_path)

    assert code == 2
    assert "Cannot replay with the recorded Policy pack" in out
    assert "---" not in out


def test_replay_with_an_edited_pack_file_is_a_pack_error(pay: Pay, cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    audit_id = pay.ask().json()["audit_record_id"]
    shutil.copytree(PACKS, tmp_path, dirs_exist_ok=True)
    pack_file = tmp_path / "default" / ACTIVE_FILE
    pack_file.write_text(
        pack_file.read_text(encoding="utf-8").replace("500000", "400000"), encoding="utf-8"
    )

    code, out = cli("replay", audit_id, "--tenant", pay.tenant_id(), packs=tmp_path)

    assert code == 2
    assert "does not match the Manifest" in out


def test_replay_prints_a_diff_when_the_recorded_output_was_altered(
    pay: Pay,
    cli,
    engine: Engine,  # type: ignore[no-untyped-def]
) -> None:
    audit_id = pay.ask().json()["audit_record_id"]
    tamper(
        engine,
        "UPDATE audit_records SET body = convert_to(replace(convert_from(body, 'UTF8'),"
        ' \'"outcome":"allow"\', \'"outcome":"block"\'), \'UTF8\') WHERE audit_record_id = :a',
        a=audit_id,
    )

    code, out = cli("replay", audit_id, "--tenant", pay.tenant_id())

    assert code == 1
    assert '-  "outcome": "block"' in out
    assert '+  "outcome": "allow"' in out


# --- verify -----------------------------------------------------------------------------


def test_an_untouched_chain_verifies_clean(pay: Pay, cli) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        pay.ask()

    code, out = cli("verify", "--tenant", pay.tenant_id())

    assert code == 0
    assert "Clean: 3 Audit records" in out


def test_an_edited_record_is_reported_at_its_seq(pay: Pay, cli, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        pay.ask()
    tamper(
        engine,
        "UPDATE audit_records SET body = convert_to(replace(convert_from(body, 'UTF8'),"
        ' \'"outcome":"allow"\', \'"outcome":"block"\'), \'UTF8\')'
        " WHERE tenant_id = :t AND seq = 2",
        t=pay.tenant_id(),
    )

    code, out = cli("verify", "--tenant", pay.tenant_id())

    assert code == 1
    assert "first fails at seq 2" in out


def test_a_deleted_record_is_reported_at_its_seq(pay: Pay, cli, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        pay.ask()
    tamper(engine, "DELETE FROM audit_records WHERE tenant_id = :t AND seq = 2", t=pay.tenant_id())

    code, out = cli("verify", "--tenant", pay.tenant_id())

    assert code == 1
    assert "first fails at seq 2" in out


def test_verify_only_reads_the_given_tenants_chain(pay: Pay, cli, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    pay.ask()
    other = Pay(pay.api, "other")
    other.ask()
    tamper(engine, "DELETE FROM audit_records WHERE tenant_id = :t", t=pay.tenant_id())

    code, out = cli("verify", "--tenant", other.tenant_id())

    assert code == 0
    assert "Clean: 1 Audit records" in out
