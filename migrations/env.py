"""Alembic environment. The first migration (Phase 1) adds tables, each with tenant_id."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from core.settings import get_settings

config = context.config
# Tests pass their own database through `config.attributes`; otherwise use settings.
url = config.attributes.get("database_url") or str(get_settings().database_url)
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # escape for configparser

target_metadata = None  # set to the SQLAlchemy MetaData once Phase 1 defines tables


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
