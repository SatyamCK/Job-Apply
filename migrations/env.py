from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from common.config import ConfigManager, load_schema
from common.config.sources import source_from_env
from common.db import models  # noqa: F401  (registers tables on Base.metadata)
from common.db.base import Base
from common.db.session import normalize_url

target_metadata = Base.metadata


def database_url() -> str:
    explicit = context.config.get_main_option("sqlalchemy.url")
    if explicit:
        return normalize_url(explicit)
    source, schema_path = source_from_env()
    manager = ConfigManager(source, load_schema(schema_path))
    return normalize_url(manager.load().secret("database.url"))


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
