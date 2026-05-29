"""Programmatic alembic runner used by ``ff-pipeline init`` and tests.

Running migrations in-process (rather than shelling out to ``alembic``)
keeps everything inside the uv-managed virtualenv and lets us inject a
SQLAlchemy connection — important for tests that use a temp-file
database without polluting global env vars.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from sqlalchemy import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def _make_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_to_head(database_url: str | None = None, *, engine: Engine | None = None) -> None:
    """Run all pending migrations to the latest revision.

    Pass an ``engine`` to migrate against an already-open SQLAlchemy
    engine (so tests can share an in-memory or temp-file connection).
    """
    cfg = _make_config(database_url)
    if engine is not None:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        return
    command.upgrade(cfg, "head")


def downgrade_to_base(database_url: str | None = None, *, engine: Engine | None = None) -> None:
    """Reverse all migrations back to the empty schema. Used by tests."""
    cfg = _make_config(database_url)
    if engine is not None:
        with engine.begin() as connection:
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
