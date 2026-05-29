"""SQLAlchemy 2.0 declarative base and engine factory.

The single source of truth for SQLAlchemy metadata. ``alembic/env.py`` and
the application both import ``Base`` from here, ensuring the autogen
migration sees every model registered against the metadata.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.event import listens_for
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the project."""


def create_app_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for the given URL.

    For SQLite URLs, the parent directory is created if missing and
    foreign-key enforcement is turned on at the connection level (SQLite
    ships with it off by default, which silently breaks ``ON DELETE`` semantics).
    """
    if database_url.startswith("sqlite"):
        db_path = _sqlite_path(database_url)
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, echo=echo, future=True)

    if engine.dialect.name == "sqlite":
        _enable_sqlite_foreign_keys(engine)

    return engine


def _sqlite_path(url: str) -> Path | None:
    # Accept ``sqlite:///relative/path.db`` and ``sqlite:////abs/path.db``.
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw).resolve()


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
