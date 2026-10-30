import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from api import admin
from tests.conftest import make_settings


def _lines(out: str) -> dict[str, str]:
    return {line.split(":")[0]: line.split()[-1] for line in out.splitlines() if ":" in line}


def _stored_keys(url: str, tenant_id: str) -> list[dict[str, object]]:
    with create_engine(url).connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM api_keys WHERE tenant_id = :t ORDER BY key_prefix"),
            {"t": tenant_id},
        )
        return [dict(r) for r in rows.mappings()]


def test_create_tenant_prints_an_agent_key_and_a_tenant_key_once_and_stores_only_hashes(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin, "get_settings", lambda: make_settings(database_url=migrated_database_url)
    )

    admin.main(["create-tenant", "Acme Wallet"])

    out = capsys.readouterr().out
    printed = _lines(out)
    agent_key, tenant_key = printed["agent_key (decide)"], printed["tenant_key (consent, manage)"]
    assert agent_key.startswith("apg_test_") and tenant_key.startswith("apg_test_")
    assert "not shown again" in out
    rows = _stored_keys(migrated_database_url, printed["tenant_id"])
    scopes = {str(r["key_prefix"]): sorted(r["scopes"]) for r in rows}  # type: ignore[call-overload]
    assert scopes == {agent_key[:16]: ["decide"], tenant_key[:16]: ["consent", "manage"]}
    stored = {str(v) for r in rows for v in r.values()}
    assert agent_key not in stored and tenant_key not in stored
    assert sum(v.startswith("sha256:") for v in stored) == 2


def test_create_key_issues_a_key_with_exactly_the_scopes_asked_for(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin, "get_settings", lambda: make_settings(database_url=migrated_database_url)
    )
    admin.main(["create-tenant", "Acme"])
    tenant_id = _lines(capsys.readouterr().out)["tenant_id"]

    assert admin.main(["create-key", "--tenant", tenant_id, "--scopes", "decide,consent"]) == 0

    key = _lines(capsys.readouterr().out)["api_key (consent, decide)"]
    rows = [
        r for r in _stored_keys(migrated_database_url, tenant_id) if r["key_prefix"] == key[:16]
    ]
    assert sorted(rows[0]["scopes"]) == ["consent", "decide"]  # type: ignore[call-overload]


def test_create_key_refuses_an_unknown_tenant_or_scope(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin, "get_settings", lambda: make_settings(database_url=migrated_database_url)
    )

    assert admin.main(["create-key", "--tenant", "ten_nope", "--scopes", "decide"]) == 3
    assert "No Tenant ten_nope" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        admin.main(["create-key", "--tenant", "ten_nope", "--scopes", "admin"])


def test_production_keys_are_live_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class Directory:
        def create_tenant(self, tenant_id, name, created_at):  # type: ignore[no-untyped-def]
            pass

        def add_api_key(self, tenant_id, key_prefix, key_hash, scopes, created_at):  # type: ignore[no-untyped-def]
            created.append(key_prefix)
            return True

    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: make_settings(env="production", anthropic_api_key=SecretStr("sk-test")),
    )
    monkeypatch.setattr(admin, "PostgresStore", lambda _engine: Directory())

    admin.main(["create-tenant", "Acme"])

    assert len(created) == 2
    assert all(prefix.startswith("apg_live_") for prefix in created)
