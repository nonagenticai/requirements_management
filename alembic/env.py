"""
Alembic configuration for asynchronous migrations.

This file sets up an async engine for running migrations, using the
``DATABASE_URL`` environment variable when available. It also provides both
online and offline migration functions. When ``DATABASE_URL`` is set, the
``sqlalchemy.url`` value defined in ``alembic.ini`` is ignored in favour of it.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import models.architecture  # noqa: F401
import models.decisions  # noqa: F401
import models.idempotency  # noqa: F401
import models.pm  # noqa: F401
import models.pm_catalogue  # noqa: F401
import models.principles  # noqa: F401
import models.quality_evaluation  # noqa: F401
import models.requirements  # noqa: F401
import models.version_history  # noqa: F401
from alembic import context
from db import Base

# Load logging configuration from this file
config = context.config
# Skip fileConfig if no formatters section exists
try:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)
except KeyError:
    # No logging configuration in alembic.ini
    pass

# Use the metadata from our models
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though
    an Engine is acceptable here as well. By skipping the Engine creation
    we don't even need a DBAPI available.
    Calls to context.execute() here emit the given string to the script
    output.
    """
    url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))
    # Convert postgresql:// to postgresql+psycopg:// for async psycopg3 driver
    if url and url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    db_url = os.getenv("DATABASE_URL", configuration.get("sqlalchemy.url"))
    # Convert postgresql:// to postgresql+psycopg:// for async psycopg3 driver
    if db_url and db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if db_url is None:
        raise RuntimeError(
            "Database URL not configured. Set DATABASE_URL environment variable "
            "or sqlalchemy.url in alembic.ini"
        )
    configuration["sqlalchemy.url"] = db_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
