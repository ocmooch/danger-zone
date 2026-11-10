"""S8 read surface on a mixed-era fixture: 2015 NFL.com (sealed), 2016 NFL.com
(open, given NFL.com-style rows here), 2026 Sleeper (the S5 fixture load)."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from ff_pipeline.api.main import create_app
from ff_pipeline.normalizer.sleeper_status import store_status_snapshots
from ff_pipeline.repository import queries as q
from ff_pipeline.repository.models import Matchup, PlayerStatsScored, TeamRoster
from ff_pipeline.repository.platform_reads import CAPABILITY_KEYS, NOT_USED, STATES
from tests.integration.test_sleeper_load import FRANCHISE, _load, db, engine  # noqa: F401
from tests.unit.test_sleeper_normalizer import OverlaySource, fixture_league

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration
NFL15, NFL16, SLEEPER = 1, 2, 3


def _legacy_rows(db: Path) -> None:  # noqa: F811
    """NFL.com-shaped 2016 data: two games, rosters with scraped nfl_com_points."""
    c = sqlite3.connect(db)
    try:
        t1, t2 = (r[0] for r in c.execute("SELECT team_id FROM teams WHERE season_id = 2"))
        pid = [r[0] for r in c.execute("SELECT player_id FROM players ORDER BY player_id LIMIT 3")]
        for week, (s1, s2) in enumerate([(100.5, 90.0), (80.0, 80.0)], start=3):
            for a, b, sa, sb in ((t1, t2, s1, s2), (t2, t1, s2, s1)):
                c.execute(
                    "INSERT INTO matchups (season_id, week, team_id, opponent_team_id, team_score,"
                    " opponent_score, is_win, is_playoff, is_consolation) VALUES (2,?,?,?,?,?,?,0,0)",
                    (week, a, b, sa, sb, None if sa == sb else sa > sb),
                )
        extra = [{"nfl_com_points": 12.3}, {"nfl_com_points": 0.0}, {}]
        for p, e in zip(pid, extra, strict=True):
            c.execute(
                "INSERT INTO team_rosters (team_id, player_id, season_year, week, roster_slot,"
                " is_starter, was_locked_at_kickoff, extra_data) VALUES (?,?,2016,3,'BN',0,1,?)",
                (t1, p, json.dumps(e)),
            )
        c.commit()
    finally:
        c.close()


@pytest.fixture
def mixed(db: Path, engine: Engine) -> Engine:  # noqa: F811
    _load(engine, OverlaySource(synthetic=True))
    _legacy_rows(db)
    return engine


def test_platform_and_freshness(mixed: Engine) -> None:
    with Session(mixed) as ss:
        assert q.season_platform(ss, NFL16) == q.PlatformInfo("nfl_com", "NFL.com")
        assert q.season_platform(ss, SLEEPER) == q.PlatformInfo("sleeper", "Sleeper")
        f = q.season_freshness(ss, SLEEPER)
        assert f is not None
        assert (f.is_final, f.last_finalized_week, f.provisional_weeks) == (False, 8, (9,))


def test_capability_matrix_per_era(mixed: Engine) -> None:
    with Session(mixed) as ss:
        nfl = q.season_capabilities(ss, NFL16)
        slp = q.season_capabilities(ss, SLEEPER)
    for caps in (nfl, slp):
        assert set(caps) == set(CAPABILITY_KEYS)
        assert {c.state for c in caps.values()} <= set(STATES)
        assert all(c.note is None for c in caps.values() if c.state == NOT_USED)  # silent
    s = {k: c.state for k, c in slp.items()}
    n = {k: c.state for k, c in nfl.items()}
    # activity: available on Sleeper even when empty; structure: present or silent
    assert (
        s["faab_failed_bids"]
        == s["trades_with_picks_faab"]
        == s["traded_future_picks"]
        == "available"
    )
    assert s["co_owners"] == "available"  # the synthetic overlay has one co-owner
    assert s["commissioner_score_overrides"] == "available"  # and one custom_points
    assert s["official_divisions"] == "not_used"
    assert s["lineup_change_log"] == "not_on_platform"
    assert s["official_brackets"] == "available"
    assert s["reconstructed_player_points"] == "not_yet"  # active season, not scored yet
    assert s["potential_points"] == "available"
    assert (n["faab_failed_bids"], n["co_owners"], n["official_brackets"]) == (
        "not_on_platform",
        "not_on_platform",
        "derived",
    )


def test_structure_without_events_resolves_silently(engine: Engine) -> None:  # noqa: F811
    _load(engine)  # no synthetic overlay: no co-owners, no overrides
    with Session(engine) as ss:
        caps = q.season_capabilities(ss, SLEEPER)
    assert caps["co_owners"] == q.Capability("not_used")
    assert caps["commissioner_score_overrides"] == q.Capability("not_used")
    assert caps["trades_with_picks_faab"].state == "available"


def _old_dashboard_points(roster: Any, scored: Any) -> Any:
    """The dashboard's expression before S8 (analytics/scoring.py)."""
    return func.coalesce(
        cast(func.json_extract(roster.extra_data, "$.nfl_com_points"), Float), scored.total_points
    )


def test_points_expression_is_identical_for_legacy_and_reads_sleeper(mixed: Engine) -> None:
    def values(expr: Any, year: int) -> list[Any]:
        return list(
            ss.execute(
                select(TeamRoster.roster_id, expr)
                .outerjoin(
                    PlayerStatsScored,
                    (PlayerStatsScored.player_id == TeamRoster.player_id)
                    & (PlayerStatsScored.week == TeamRoster.week),
                )
                .where(TeamRoster.season_year == year)
                .order_by(TeamRoster.roster_id)
            ).tuples()
        )

    with Session(mixed) as ss:
        new = q.authoritative_week_points_expr(TeamRoster, PlayerStatsScored)
        old = _old_dashboard_points(TeamRoster, PlayerStatsScored)
        legacy_new = values(new, 2016)
        assert legacy_new == values(old, 2016)
        assert [v for _, v in legacy_new][-3:] == [12.3, 0.0, None]  # the rows added above
        rows = ss.execute(
            select(q.official_slot_points_expr(TeamRoster), TeamRoster.extra_data).where(
                TeamRoster.season_year == 2026, TeamRoster.week >= 1
            )
        ).tuples()
        assert all(v == e["platform_points"] for v, e in rows)


def _old_standings(ss: Session, season_id: int) -> dict[int, tuple[Any, ...]]:
    """``standings_for_season`` before S8, for the legacy parity check."""
    by: dict[int, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])
    for m in ss.scalars(select(Matchup).where(Matchup.season_id == season_id)):
        r = by[m.team_id]
        if m.is_win is True:
            r[0] += 1
        elif m.is_win is False:
            r[1] += 1
        elif m.team_score is not None and m.opponent_score is not None:
            r[2] += 1
        r[3] += m.team_score or 0.0
        r[4] += m.opponent_score or 0.0
    return {
        t: (int(r[0]), int(r[1]), int(r[2]), round(r[3], 2), round(r[4], 2)) for t, r in by.items()
    }


def test_standings_legacy_unchanged_sleeper_current_state(mixed: Engine) -> None:
    keys = ("wins", "losses", "ties", "points_for", "points_against")
    with Session(mixed) as ss:
        for sid in (NFL15, NFL16):
            rows = q.standings_for_season(ss, sid)
            assert {r["team_id"]: tuple(r[k] for k in keys) for r in rows} == _old_standings(
                ss, sid
            )
            assert not any(r["provisional"] or r["live_week"] for r in rows)
        rows = q.standings_for_season(ss, SLEEPER)
    assert len(rows) == 12
    assert all(r["wins"] + r["losses"] + r["ties"] == 8 for r in rows)  # weeks 1-8 final
    assert all(r["provisional"] and r["live_week"] == 9 for r in rows)
    assert all(r["live_points_for"] is not None for r in rows)


def test_failed_claims_and_trade_assets_are_included(mixed: Engine) -> None:
    with Session(mixed) as ss:
        failed = q.failed_waiver_claims(ss, SLEEPER)
        assert failed and all(t.status == "failed" for t in failed)
        team = failed[0].team_id
        assert team is not None
        listed = q.transactions_for_team(ss, team)
        assert {t.transaction_id for t in failed if t.team_id == team} <= {
            t.transaction_id for t in listed
        }  # S8-D4: included by default
        assets = q.trade_assets(ss, SLEEPER)
        assert len(assets) == 2 and all(t.player_id is None for t in assets)
        assert q.list_transactions(ss, season_year=2026, limit=5000)  # player-less legs ok
        assert len(q.brackets_for_season(ss, SLEEPER)) == 14
        (pick,) = q.traded_picks_for_league(ss, FRANCHISE)
        assert pick.pick_season == 2027


def test_status_as_of_respects_the_staleness_cutoff(mixed: Engine) -> None:
    data = fixture_league()
    with Session(mixed) as ss:
        from ff_pipeline.crawlers.sleeper_league.runner import build_identity_maps

        ids, _ = build_identity_maps(ss, data, league_id=FRANCHISE)
        store_status_snapshots(ss, [(date(2026, 11, 4), data.players)], player_ids=ids.player_ids)
        ss.commit()
        pid = next(iter(ids.player_ids.values()))
        assert pid in q.player_status_asof(ss, [pid], date(2026, 11, 8))
        assert pid in q.player_status_asof(ss, [pid], date(2026, 11, 11))  # 7 days
        assert pid not in q.player_status_asof(ss, [pid], date(2026, 11, 12))  # stale
        assert pid not in q.player_status_asof(ss, [pid], date(2026, 11, 3))  # before


def test_api_exposes_platform_capabilities_and_new_reads(mixed: Engine) -> None:
    client = TestClient(create_app(engine=mixed))
    s = client.get(f"/seasons/{SLEEPER}").json()["data"]
    assert (s["platform"], s["platform_label"], s["is_final"]) == ("sleeper", "Sleeper", False)
    assert s["provisional_weeks"] == [9]
    assert set(s["capabilities"]) == set(CAPABILITY_KEYS)
    legacy = client.get(f"/seasons/{NFL16}").json()["data"]
    assert (legacy["platform"], legacy["platform_label"]) == ("nfl_com", "NFL.com")
    listed = client.get(f"/leagues/{FRANCHISE}/seasons").json()["data"]
    assert {x["year"]: x["platform_label"] for x in listed}[2026] == "Sleeper"
    assert len(client.get(f"/seasons/{SLEEPER}/brackets").json()["data"]) == 14
    assert client.get(f"/leagues/{FRANCHISE}/traded-picks").json()["data"][0]["round"] == 2
    st = client.get(f"/seasons/{SLEEPER}/standings").json()["data"]["rows"]
    assert st[0]["provisional"] is True and st[0]["live_week"] == 9
    with Session(mixed) as ss:
        failed_team = q.failed_waiver_claims(ss, SLEEPER)[0].team_id
    tx = client.get(
        "/transactions", params={"season": 2026, "team_id": failed_team, "limit": 500}
    ).json()["data"]
    assert "failed" in {t["status"] for t in tx}
    with Session(mixed) as ss:
        pid = ss.scalar(select(TeamRoster.player_id).where(TeamRoster.season_year == 2026))
    assert client.get(f"/players/{pid}/status").json()["data"] is None  # no snapshot: a gap
