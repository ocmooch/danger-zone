"""Programmatic alembic runner used by ``ff-pipeline init`` and tests.

Running migrations in-process (rather than shelling out to ``alembic``)
keeps everything inside the uv-managed virtualenv and lets us inject a
SQLAlchemy connection — important for tests that use a temp-file
database without polluting global env vars.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def _make_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@contextmanager
def _fk_disabled_for_sqlite(connection: Connection) -> Iterator[None]:
    """Turn off SQLite FK enforcement for the duration of a migration.

    SQLite can't add/drop a column constraint in place, so Alembic's batch
    mode recreates the whole table (CREATE tmp → copy → DROP original →
    RENAME). The ``DROP`` of a *referenced* table (e.g. ``teams``) trips the
    child FKs that the app turns on via ``PRAGMA foreign_keys=ON``.
    ``foreign_keys`` can't be toggled inside a transaction, so we flip it on
    the raw DBAPI connection *before* Alembic opens its transaction, then
    restore it. Non-SQLite backends are untouched.
    """
    dbapi = connection.connection.dbapi_connection
    if connection.dialect.name != "sqlite" or dbapi is None:
        yield
        return
    dbapi.execute("PRAGMA foreign_keys=OFF")
    try:
        yield
    finally:
        dbapi.execute("PRAGMA foreign_keys=ON")


def upgrade_to_head(database_url: str | None = None, *, engine: Engine | None = None) -> None:
    """Run all pending migrations to the latest revision.

    Pass an ``engine`` to migrate against an already-open SQLAlchemy
    engine (so tests can share an in-memory or temp-file connection).
    """
    cfg = _make_config(database_url)
    if engine is not None:
        # connect() (not begin()) so FK enforcement can be toggled before
        # Alembic opens its own transaction; the env's begin_transaction()
        # commits the migration.
        with engine.connect() as connection, _fk_disabled_for_sqlite(connection):
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        return
    command.upgrade(cfg, "head")


def downgrade_to_base(database_url: str | None = None, *, engine: Engine | None = None) -> None:
    """Reverse all migrations back to the empty schema. Used by tests."""
    cfg = _make_config(database_url)
    if engine is not None:
        with engine.connect() as connection, _fk_disabled_for_sqlite(connection):
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "base")
        return
    command.downgrade(cfg, "base")


def current_revision(database_url: str | None = None, *, engine: Engine | None = None) -> None:
    """Print the current migration revision (used by ``ff-pipeline migrate status``)."""
    cfg = _make_config(database_url)
    if engine is not None:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.current(cfg, verbose=True)
        return
    command.current(cfg, verbose=True)
