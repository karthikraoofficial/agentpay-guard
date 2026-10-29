from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def test_a_fresh_database_migrates_to_head(migrated_database_url: str) -> None:
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    with create_engine(migrated_database_url).connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert current == head


def test_0007_backfills_each_decisions_merchant_from_its_audit_record() -> None:
    from alembic import command
    from fastapi.testclient import TestClient
    from sqlalchemy import make_url

    from adapters.memory.model_gateway import ScriptedGateway
    from adapters.memory.telemetry import InMemoryTelemetry
    from adapters.postgres.store import PostgresStore
    from api.main import create_app
    from core.settings import get_settings
    from tests.api.conftest import NOW, Api, MovableClock
    from tests.api.test_decisions import Pay
    from tests.conftest import make_settings

    configured = make_url(str(get_settings().database_url))
    url = configured.set(database=f"{configured.database}_mig_test")
    admin = create_engine(configured.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    config = Config("alembic.ini")
    config.attributes["database_url"] = url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    engine = create_engine(url)
    store = PostgresStore(engine)
    clock = MovableClock(NOW)
    app = create_app(
        make_settings(),
        store=store,
        clock=clock,
        telemetry=InMemoryTelemetry(),
        gateway=ScriptedGateway(),
    )
    pay = Pay(Api(TestClient(app), store, clock))
    pay.ask(merchant={"id": "m_backfill", "name": "Shop", "mcc": "5411"})
    engine.dispose()

    command.downgrade(config, "0006_intent_checks")
    command.upgrade(config, "head")

    with create_engine(url).connect() as conn:
        merchants = conn.execute(text("SELECT merchant_id FROM decisions")).scalars().all()
    assert merchants == ["m_backfill"]
