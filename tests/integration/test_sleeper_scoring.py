"""S6 end to end on the sealed fixture DB: rules → Sleeper stats → reconcile,
and gsis adoption for a rookie split across two player rows."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.sleeper_league.client import FixtureSleeperLeagueSource
from ff_pipeline.crawlers.sleeper_league.stats import store_weekly_stats
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.normalizer.gsis_adoption import apply_adoptions, plan_adoptions
from ff_pipeline.repository.models import (
    Player,
    PlayerIdentityLink,
    PlayerStatsRaw,
    PlayerStatsScored,
    TeamRoster,
)
from ff_pipeline.scoring.platform_reconcile import (
    MATCH,
    RULE,
    STAT_SOURCE,
    reconcile_platform_points,
)
from ff_pipeline.scoring.rescore import rescore_seasons
from ff_pipeline.scoring.sleeper_rules import derive_rules, write_season_rules
from tests.integration.test_sleeper_load import FRANCHISE, _load, db, engine  # noqa: F401
from tests.unit.test_sleeper_normalizer import FX, fixture_league

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

SEASON_ID = 3  # the 2026 Sleeper season row the S5 fixture creates


def _score(eng: Engine) -> None:
    _load(eng)
    with Session(eng) as ss:
        write_season_rules(ss, SEASON_ID, derive_rules(fixture_league().league["scoring_settings"]))
        out = store_weekly_stats(
            ss, FixtureSleeperLeagueSource(FX / "season_2026"), season=2026, weeks=range(1, 10)
        )
        assert out.weeks == tuple(range(1, 10)) and out.rows_added > 1000
        ss.commit()


def test_sleeper_stats_are_never_the_baseline_and_reproduce_official(
    db: Path,  # noqa: F811
    engine: Engine,  # noqa: F811
) -> None:
    _score(engine)
    with Session(engine) as ss:
        res = rescore_seasons(ss, season_years=[2026], league_id=FRANCHISE)
        ss.commit()
        assert res.rows_scored == 0  # only nflverse rows are scored
        assert ss.scalar(select(func.count()).select_from(PlayerStatsScored)) == 0
        assert not ss.scalar(
            select(func.count()).where(
                PlayerStatsRaw.source == "sleeper", PlayerStatsRaw.is_primary.is_(True)
            )
        )
        rec = reconcile_platform_points(ss, league_id=FRANCHISE, season_year=2026)
    # with no nflverse rows, every non-zero official score is "stat source": our
    # engine over Sleeper's own stats reproduces it; none is a rule error
    counts = rec.counts(starters_only=False)
    assert counts[RULE] == 0
    assert counts[STAT_SOURCE] > 500
    assert all(c.cls == MATCH for c in rec.rows(starters_only=False) if c.official == 0)
    assert rec.finalized_weeks == tuple(range(1, 9))
    assert rec.rows(starters_only=False, provisional=True)  # week 9
    conn = connect(str(db))
    try:
        assert verify(conn, full=True).ok
    finally:
        conn.close()


def test_rules_rewrite_is_idempotent(engine: Engine) -> None:  # noqa: F811
    _score(engine)
    with Session(engine) as ss:
        rows = derive_rules(fixture_league().league["scoring_settings"])
        assert write_season_rules(ss, SEASON_ID, rows) == len(rows) == 58
        ss.commit()


def test_gsis_adoption_moves_the_real_id_and_stats(db: Path, engine: Engine) -> None:  # noqa: F811
    _load(engine)
    with Session(engine) as ss:
        keep = ss.scalars(
            select(Player)
            .join(TeamRoster, TeamRoster.player_id == Player.player_id)
            .where(TeamRoster.season_year == 2026, Player.position == "WR")
            .order_by(Player.player_id)
        ).first()
        assert keep is not None
        keep.gsis_id = "ABC123456"  # a pre-draft placeholder id
        twin = Player(name_full=keep.name_full, position="WR", gsis_id="00-0099999", is_active=True)
        ss.add(twin)
        ss.flush()
        ss.add(
            PlayerStatsRaw(
                player_id=twin.player_id,
                season_year=2026,
                week=1,
                source="nflverse",
                stats={"receptions": 3.0},
                is_primary=True,
            )
        )
        ss.commit()
        keep_id, twin_id = keep.player_id, twin.player_id

        plan = plan_adoptions(ss, season=2026)
        assert [(a.keep_id, a.twin_id, a.gsis_id) for a in plan.adoptions] == [
            (keep_id, twin_id, "00-0099999")
        ]
        apply_adoptions(ss, plan.adoptions)
        ss.commit()
        assert plan_adoptions(ss, season=2026).adoptions == []

    c = sqlite3.connect(db)
    try:
        assert c.execute(
            "SELECT gsis_id FROM players WHERE player_id IN (?, ?) ORDER BY player_id",
            (keep_id, twin_id),
        ).fetchall() == [("00-0099999",), (None,)]
        assert c.execute(
            "SELECT player_id FROM player_stats_raw WHERE source = 'nflverse' AND week = 1"
            " AND season_year = 2026"
        ).fetchall() == [(keep_id,)]
    finally:
        c.close()
    with Session(engine) as ss:
        link = ss.scalars(
            select(PlayerIdentityLink).where(PlayerIdentityLink.member_player_id == twin_id)
        ).one()
        assert link.canonical_player_id == keep_id
        assert "ABC123456" in (link.notes or "")
