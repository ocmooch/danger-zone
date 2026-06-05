"""FastAPI application factory.

Routes are split into modules under ``ff_pipeline.api.routes``; the
factory wires them all up. Tests construct an app with a custom engine
(``create_app(engine=tmp_engine)``) so the same code path serves
production and integration tests without monkey-patching.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from ff_pipeline.api.errors import install_error_handlers
from ff_pipeline.api.routes import (
    assets,
    health,
    leagues,
    matchups,
    owners,
    players,
    scoring_rules,
    seasons,
    stats,
    teams,
    transactions,
)
from ff_pipeline.repository.database import create_app_engine

if TYPE_CHECKING:
    from sqlalchemy import Engine

API_TITLE = "ff-pipeline read API"
API_VERSION = "v1"


def create_app(engine: Engine | None = None, *, assets_dir: Path | None = None) -> FastAPI:
    """Build the FastAPI app, optionally bound to a custom engine.

    A pre-built engine lets the integration tests use a temp-file SQLite
    database without monkey-patching settings. In production the CLI
    leaves ``engine=None`` and we derive one (and the assets root) from
    ``Settings``. ``assets_dir`` is where ``/assets/{id}`` streams bytes
    from; tests pass a temp dir, prod derives it from settings.
    """
    if engine is None:
        from ff_pipeline.settings import get_settings

        settings = get_settings()
        engine = create_app_engine(settings.database_url)
        if assets_dir is None:
            assets_dir = settings.assets_dir

    app = FastAPI(title=API_TITLE, version=API_VERSION)
    app.state.engine = engine
    app.state.assets_dir = assets_dir or Path("data/assets")

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(leagues.router)
    app.include_router(seasons.router)
    app.include_router(teams.router)
    app.include_router(owners.router)
    app.include_router(players.router)
    app.include_router(matchups.router)
    app.include_router(transactions.router)
    app.include_router(scoring_rules.router)
    app.include_router(stats.router)
    app.include_router(assets.router)

    return app
