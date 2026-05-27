"""Alembic environment.

Resolves the database URL in this order:

1. The URL programmatically set by application code via
   ``context.config.attributes["connection"]`` (preferred — used by the
   in-process migration runner in ``repository.migrations``).
2. The ``DATABASE_URL`` environment variable (set by ``.env`` via
   ``pydantic-settings`` in normal operation, or by the user when
   running ``alembic`` directly).
3. The ``sqlalchemy.url`` value baked into ``alembic.ini`` (the
   SQLite fallback).

This lets the same migration set run programmatically (M1's ``ff-pipeline
init``) and from the ``alembic`` CLI for ad-hoc debugging.
"""

from __future__ import annotations

import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ff_pipeline.repository import models as _models  # noqa: F401  — register tables
from ff_pipeline.repository.database import Base

config = context.config

# Only let alembic install its own loggers when nothing else has done so
# (e.g., running `alembic` from the CLI for ad-hoc debugging). Calling
# ff-pipeline configures logging via ff_pipeline.logging_config before
# alembic runs, and fileConfig would wipe those handlers.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Allow the env var to override the ini-baked URL.
_env_url = os.environ.get("DATABASE_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    # Prefer a connection injected by application code (in-process migration).
    injected = config.attributes.get("connection")
    if injected is not None:
        context.configure(
            connection=injected,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
