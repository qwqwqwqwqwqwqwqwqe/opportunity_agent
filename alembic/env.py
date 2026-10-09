from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from opportunity_agent.v2.db.base import Base
from opportunity_agent.v2.db import models  # noqa: F401 - registers metadata

config = context.config
target_metadata = Base.metadata
database_url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    if connection.dialect.name == "postgresql":
        # Alembic's default version table uses VARCHAR(32), while existing
        # revision identifiers in this project are longer. Prepare/expand only
        # Alembic's bookkeeping column before it records the next revision.
        inspector = inspect(connection)
        if not inspector.has_table("alembic_version"):
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
            )
        else:
            column = next(item for item in inspector.get_columns("alembic_version")
                          if item["name"] == "version_num")
            if 0 < (getattr(column["type"], "length", None) or 0) < 64:
                connection.exec_driver_sql(
                    "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"
                )
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
