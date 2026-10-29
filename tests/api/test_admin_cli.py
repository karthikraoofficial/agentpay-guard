import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from api import admin
from tests.conftest import make_settings


def test_create_tenant_prints_the_key_once_and_stores_only_its_hash(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin, "get_settings", lambda: make_settings(database_url=migrated_database_url)
    )

    admin.main(["create-tenant", "Acme Wallet"])

    out = capsys.readouterr().out
    key = next(line.split()[-1] for line in out.splitlines() if line.startswith("api_key:"))
    tenant_id = next(line.split()[-1] for line in out.splitlines() if line.startswith("tenant_id:"))
    assert key.startswith("apg_test_")
    assert "not shown again" in out
    with create_engine(migrated_database_url).connect() as conn:
        rows = (
            conn.execute(text("SELECT * FROM api_keys WHERE tenant_id = :t"), {"t": tenant_id})
            .mappings()
            .all()
        )
    assert len(rows) == 1
    stored = {str(v) for v in rows[0].values()}
    assert key not in stored
    assert key[:16] in stored
    assert any(v.startswith("sha256:") for v in stored)


def test_production_keys_are_live_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class Directory:
        def create_tenant(self, tenant_id, name, key_prefix, key_hash, created_at):  # type: ignore[no-untyped-def]
            created.append(key_prefix)

    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: make_settings(env="production", anthropic_api_key=SecretStr("sk-test")),
    )
    monkeypatch.setattr(admin, "PostgresStore", lambda _engine: Directory())

    admin.main(["create-tenant", "Acme"])

    assert created[0].startswith("apg_live_")
