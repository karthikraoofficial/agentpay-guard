"""`python -m api.admin verify-pack`: rebuild an Evidence pack's bundle from the Audit chain
and check every stored version still matches it, against Postgres with edits made as a
superuser would make them."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from adapters.memory.rail import MockRail
from adapters.postgres.store import PostgresStore
from api.main import create_app
from tests.api.conftest import NOW, Api, MovableClock
from tests.api.test_disputes import DISPUTE_RULES, Disputes
from tests.api.test_rail_webhooks import SECRET
from tests.api.test_replay_verify_cli import run, tamper
from tests.conftest import make_settings
from tests.packs import release_test_pack


@pytest.fixture
def engine(migrated_database_url: str) -> Engine:
    return create_engine(migrated_database_url)


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    return release_test_pack(tmp_path / "packs", "1.0.1", {"disputes": DISPUTE_RULES})


@pytest.fixture
def disputes(engine: Engine, packs: Path) -> Disputes:
    store = PostgresStore(engine)
    clock = MovableClock(NOW)
    api = Api(
        TestClient(create_app(make_settings(), store=store, clock=clock)),
        store,
        clock,
        rail=MockRail(),
    )
    api.use_packs(packs)
    api.use_settings(make_settings(razorpay_webhook_secret=SECRET))
    return Disputes(api)


@pytest.fixture
def cli(monkeypatch, capsys, migrated_database_url: str, packs: Path):  # type: ignore[no-untyped-def]
    def call(*argv: str) -> tuple[int, str]:
        return run(monkeypatch, capsys, migrated_database_url, *argv, packs=packs)

    return call


def _signed(disputes: Disputes) -> dict:  # type: ignore[type-arg]
    dispute = disputes.built(disputes.captured())
    disputes.act(dispute["dispute_id"], "regenerate")
    disputes.build(dispute["dispute_id"])
    disputes.sign_off(dispute["dispute_id"], pack_version=2)
    return dispute


def _edit_pack(engine: Engine, dispute_id: str, version: int, old: str, new: str) -> None:
    tamper(
        engine,
        "UPDATE evidence_packs SET body = convert_to(replace(convert_from(body, 'UTF8'),"
        " :old, :new), 'UTF8') WHERE dispute_id = :d AND version = :v",
        old=old,
        new=new,
        d=dispute_id,
        v=version,
    )


def test_an_untouched_signed_off_dispute_verifies(disputes: Disputes, cli) -> None:  # type: ignore[no-untyped-def]
    dispute = _signed(disputes)

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 0, out
    assert "Pack v1: matches the Audit chain" in out
    assert "Pack v2: matches the Audit chain" in out
    assert "Pack v2: matches its Sign-off" in out
    assert "Clean:" in out


def test_an_edited_narrative_on_a_signed_off_version_is_caught(
    disputes: Disputes,
    cli,
    engine: Engine,  # type: ignore[no-untyped-def]
) -> None:
    dispute = _signed(disputes)
    _edit_pack(engine, dispute["dispute_id"], 2, "was captured", "was refunded")

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 1
    assert "Pack v2: differs from the pack hash its Sign-off recorded" in out
    assert "Pack v2: the narrative written by code differs" in out


def test_an_edited_assessment_is_caught(disputes: Disputes, cli, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    dispute = disputes.built(disputes.captured())
    _edit_pack(engine, dispute["dispute_id"], 1, '"strength":"mixed"', '"strength":"strong"')

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 1
    assert "Pack v1: the assessment differs from default@1.0.1's Dispute rules" in out


def test_an_edited_audit_record_is_caught_in_the_bundle_and_the_chain(
    disputes: Disputes,
    cli,
    engine: Engine,  # type: ignore[no-untyped-def]
) -> None:
    dispute = disputes.built(disputes.captured())
    tamper(
        engine,
        "UPDATE audit_records SET body = convert_to(replace(convert_from(body, 'UTF8'),"
        ' \'"name":"FreshMart"\', \'"name":"FreshMarr"\'), \'UTF8\')'
        " WHERE decision_id = :d AND kind = 'decision'",
        d=dispute["decision_id"],
    )

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 1
    assert "Pack v1: its bundle differs from the one the Audit chain gives" in out
    assert "Broken: the Audit chain" in out


def test_an_unreadable_pack_is_reported(disputes: Disputes, cli, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    dispute = disputes.built(disputes.captured())
    _edit_pack(
        engine, dispute["dispute_id"], 1, '"bundle_hash":"sha256:', '"bundle_hash":"sha256:0'
    )

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 1
    assert "Pack v1: can't be read" in out


def test_a_dispute_without_a_pack_yet_verifies_its_chain(disputes: Disputes, cli) -> None:  # type: ignore[no-untyped-def]
    dispute = disputes.open(disputes.captured()["payment_id"]).json()

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 0, out
    assert "no pack built yet" in out


def test_a_missing_policy_pack_is_a_pack_problem(
    disputes: Disputes,
    cli,
    packs: Path,  # type: ignore[no-untyped-def]
) -> None:
    dispute = disputes.built(disputes.captured())
    manifest = json.loads((packs / "manifest.json").read_text(encoding="utf-8"))
    del manifest["packs"]["default"]["versions"]["1.0.1"]
    manifest["packs"]["default"]["active"] = "1.0.0"
    (packs / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", disputes.tenant_id())

    assert code == 2
    assert "default@1.0.1" in out


def test_another_tenants_dispute_is_not_found(disputes: Disputes, cli) -> None:  # type: ignore[no-untyped-def]
    dispute = disputes.built(disputes.captured())
    other = Disputes(disputes.api, "globex")

    code, out = cli("verify-pack", dispute["dispute_id"], "--tenant", other.tenant_id())

    assert code == 3
    assert "No Dispute" in out
