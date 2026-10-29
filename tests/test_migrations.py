from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def test_a_fresh_database_migrates_to_head(migrated_database_url: str) -> None:
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    with create_engine(migrated_database_url).connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert current == head
