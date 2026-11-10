"""Sleeper season → canonical tables, end to end on a sealed fixture DB (S5)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session

from ff_pipeline.archive.raw_store import RawStore
from ff_pipeline.canonical.loader import PlatformMismatchError, load_season
from ff_pipeline.crawlers.sleeper_league.capture import capture_season
from ff_pipeline.crawlers.sleeper_league.client import (
    ArchiveSleeperLeagueSource,
    FixtureSleeperLeagueSource,
)
from ff_pipeline.crawlers.sleeper_league.runner import load_league_season
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.normalizer.sleeper_league import normalize
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.legacy_guard import SealedSeasonError
from ff_pipeline.repository.models import Owner, OwnerPlatformIdentity, Player
from tests.integration.test_legacy_guard import _all_fingerprints
from tests.integration.test_legacy_guard import _build as build_sealed_db
from tests.unit.test_sleeper_normalizer import (
    FX,
    LEAGUE,
    OverlaySource,
    fixture_ids,
    fixture_league,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

FRANCHISE = "L"  # the sealed fixture's league key
SNAPSHOT_SQL = {
    "teams": "SELECT team_id, owner_id, team_name, team_abbrev, draft_position, platform_team_key,"
    " regular_season_wins, regular_season_losses, regular_season_ties,"
    " regular_season_points_for, regular_season_points_against FROM teams WHERE season_id = 3",
    "matchups": "SELECT matchup_id, week, team_id, opponent_team_id, team_score, opponent_score,"
    " is_win, platform_matchup_key, extra_data FROM matchups WHERE season_id = 3",
    "team_rosters": "SELECT roster_id, team_id, player_id, week, roster_slot, is_starter,"
    " was_locked_at_kickoff, extra_data FROM team_rosters WHERE season_year = 2026",
    "transactions": "SELECT transaction_id, transaction_type, direction, team_id,"
    " counterpart_team_id, player_id, executed_at, effective_week, status, notes, extra_data,"
    " platform_transaction_id FROM transactions WHERE season_id = 3",
}


def _snapshot(db: Path) -> dict[str, list[tuple[Any, ...]]]:
    c = sqlite3.connect(db)
    try:
        return {k: sorted(c.execute(q).fetchall(), key=repr) for k, q in SNAPSHOT_SQL.items()}
    finally:
        c.close()


def _prepare(db: Path) -> None:
    """2026 Sleeper season row, 12 owners + identities, fixture players."""
    data = fixture_league()
    ids = fixture_ids(data)
    c = sqlite3.connect(db)
    try:
        c.execute(
            "INSERT INTO seasons (season_id, league_id, year, status, platform)"
            " VALUES (3, 'L', 2026, 'in_progress', 'sleeper')"
        )
        c.commit()
    finally:
        c.close()
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        with Session(engine) as ss:
            for user_id, owner_id in ids.owner_by_user.items():
                ss.add(
                    Owner(owner_id=owner_id, league_id=FRANCHISE, display_name=f"Owner {owner_id}")
                )
                ss.add(
                    OwnerPlatformIdentity(
                        owner_id=owner_id, platform="sleeper", external_user_id=user_id
                    )
                )
            for sid in sorted(set(data.players) | {str(p["player_id"]) for p in data.draft_picks}):
                info = data.players.get(sid) or {}
                name = " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x)
                ss.add(
                    Player(
                        name_full=name or f"Drafted {sid}",
                        position=info.get("position"),
                        nfl_team=info.get("team"),
                        sleeper_id=sid,
                        is_active=True,
                    )
                )
            ss.commit()
    finally:
        engine.dispose()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = build_sealed_db(tmp_path)  # 2015 sealed, 2016 open (NFL.com)
    _prepare(path)
    return path


@pytest.fixture
def engine(db: Path) -> Iterator[Engine]:
    eng = create_app_engine(f"sqlite:///{db}")
    yield eng
    eng.dispose()


def _load(engine: Engine, source: Any = None) -> Any:
    with Session(engine) as ss:
        out = load_league_season(
            ss, source or OverlaySource(), LEAGUE, franchise_league_id=FRANCHISE
        )
        ss.commit()
    return out


def test_load_is_idempotent_and_matches_sleeper_records(db: Path, engine: Engine) -> None:
    first = _load(engine)
    r = first.result
    assert {k: c.inserted for k, c in r.counts.items()} == {
        "teams": 12,
        "matchups": 108,
        "team_rosters": sum(c.inserted for c in [r.counts["team_rosters"]]),
        "transactions": r.counts["transactions"].inserted,
    }
    assert r.counts["team_rosters"].inserted > 1500
    assert r.record_mismatches == []
    snap = _snapshot(db)
    second = _load(engine).result
    for name, c in second.counts.items():
        assert (c.inserted, c.updated, c.deleted) == (0, 0, 0), name
    assert _snapshot(db) == snap


def test_one_owner_per_player_per_week(db: Path, engine: Engine) -> None:
    _load(engine)
    c = sqlite3.connect(db)
    try:
        dupes = c.execute(
            "SELECT count(*) FROM (SELECT week, player_id FROM team_rosters"
            " WHERE season_year = 2026 GROUP BY week, player_id HAVING count(*) > 1)"
        ).fetchone()[0]
    finally:
        c.close()
    assert dupes == 0


def test_changed_week_updates_only_that_week(db: Path, engine: Engine) -> None:
    _load(engine)
    before = _snapshot(db)
    data = fixture_league(synthetic=True)  # synthetic week-1 matchups carry custom_points
    with Session(engine) as ss:
        from ff_pipeline.crawlers.sleeper_league.runner import build_identity_maps

        ids, _ = build_identity_maps(ss, data, league_id=FRANCHISE)
        res = load_season(ss, normalize(data, ids), league_id=FRANCHISE)
        ss.commit()
    # the override changes the team's score and its opponent's opponent_score
    assert res.counts["matchups"].updated == 2
    after = _snapshot(db)
    changed = [r for r in after["matchups"] if r not in before["matchups"]]
    assert len(changed) == 2
    assert {r[1] for r in changed} == {1}  # week 1 only
    assert {changed[0][2], changed[0][3]} == {changed[1][2], changed[1][3]}  # the pair


def test_sealed_data_untouched_and_verify_passes(db: Path, engine: Engine) -> None:
    sealed_before = {
        t: (v.get("by_season") or {}).get("2015") for t, v in _all_fingerprints(db).items()
    }
    out = _load(engine)
    sealed_after = {
        t: (v.get("by_season") or {}).get("2015") for t, v in _all_fingerprints(db).items()
    }
    assert sealed_after == sealed_before
    conn = connect(str(db))
    try:
        assert verify(conn, full=True).ok
    finally:
        conn.close()
    # sealed owners 1 and 2 have no 2026 team: the recompute reports, never writes
    assert {line.split(":")[0] for line in out.result.owner_blocked} == {"owner 1", "owner 2"}
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT is_active FROM owners WHERE owner_id IN (1, 2)").fetchall() == [
            (1,),
            (1,),
        ]
    finally:
        c.close()


def test_sealed_and_wrong_platform_seasons_are_refused(engine: Engine) -> None:
    data = fixture_league()
    with Session(engine) as ss:
        from ff_pipeline.crawlers.sleeper_league.runner import build_identity_maps

        ids, _ = build_identity_maps(ss, data, league_id=FRANCHISE)
        bundle = normalize(data, ids)
        from dataclasses import replace

        bundle.season = replace(bundle.season, year=2015)
        with pytest.raises(SealedSeasonError):
            load_season(ss, bundle, league_id=FRANCHISE)
        ss.rollback()
        bundle.season = replace(bundle.season, year=2016)
        with pytest.raises(PlatformMismatchError):
            load_season(ss, bundle, league_id=FRANCHISE)
        ss.rollback()


def test_archive_load_equals_fixture_load(tmp_path: Path, db: Path, engine: Engine) -> None:
    store = RawStore(tmp_path / "raw")
    with Session(engine) as ss:
        capture_season(
            ss,
            FixtureSleeperLeagueSource(FX / "season_2026"),
            store,
            start_league_id=LEAGUE,
            season=2026,
            run_id="fx",
            now=datetime(2026, 11, 10, tzinfo=UTC),
        )
        ss.commit()
    _load(engine, ArchiveSleeperLeagueSource(Session(engine), store))
    from_archive = _snapshot(db)
    second = _load(engine)  # fixture source over the same payloads
    for name, c in second.result.counts.items():
        assert (c.inserted, c.updated, c.deleted) == (0, 0, 0), name
    assert _snapshot(db) == from_archive
    assert json.dumps(from_archive, default=str)
