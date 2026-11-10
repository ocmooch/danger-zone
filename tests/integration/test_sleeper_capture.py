"""Sleeper league capture end to end on fixtures (S2).

A synthetic, pseudonymous 3-season chain (2026 ← 2027 ← 2028, state = 2028
week 3) is written to a temp dir; capture archives it into a migrated temp
DB + raw dir. Real (pseudonymized) captured fixtures live under
``tests/fixtures/sleeper_league/`` once S2's live capture has run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.archive.raw_store import RawStore
from ff_pipeline.crawlers.sleeper_league.capture import capture_season
from ff_pipeline.crawlers.sleeper_league.client import (
    ArchiveMissError,
    ArchiveSleeperLeagueSource,
    FetchedPayload,
    FixtureSleeperLeagueSource,
    TransientHTTPError,
)
from ff_pipeline.crawlers.sleeper_league.endpoints import (
    Endpoint,
    PayloadRequest,
    league_request,
    league_requests,
    players_request,
)
from ff_pipeline.legacy.verify import connect, verify
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import _fk_disabled_for_sqlite, _make_config, upgrade_to_head
from ff_pipeline.repository.models import SourcePayload
from tests.integration.test_legacy_guard import _all_fingerprints
from tests.integration.test_legacy_guard import _build as build_sealed_db

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

L26, L27, L28 = "900000000000002026", "900000000000002027", "900000000000002028"
DRAFT = "800000000000002028"
WEEKS = 3
T0 = datetime(2028, 9, 25, 12, 0, tzinfo=UTC)


def _write(directory: Path, req: PayloadRequest, body: Any) -> None:
    (directory / req.fixture_name).write_text(json.dumps(body))


def write_chain(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "v1_state_nfl.json").write_text(
        json.dumps({"season": "2028", "season_type": "regular", "week": WEEKS, "leg": WEEKS})
    )
    for lid, season, prev in ((L28, 2028, L27), (L27, 2027, L26), (L26, 2026, "0")):
        _write(
            directory,
            league_request(lid),
            {
                "league_id": lid,
                "season": str(season),
                "previous_league_id": prev,
                "status": "in_season" if season == 2028 else "complete",
                "name": "Fixture League",
                "draft_id": DRAFT if season == 2028 else None,
            },
        )
    (directory / "v1_league_900000000000002028_drafts.json").write_text(
        json.dumps([{"draft_id": DRAFT, "season": "2028"}])
    )
    (directory / f"v1_draft_{DRAFT}.json").write_text(json.dumps({"draft_id": DRAFT}))
    (directory / f"v1_draft_{DRAFT}_picks.json").write_text(json.dumps([]))
    for req in league_requests(L28, 2028, WEEKS):
        body: Any = []
        if req.endpoint is Endpoint.MATCHUPS:
            body = [{"roster_id": 1, "matchup_id": 1, "points": 100.5 + (req.week or 0)}]
        elif req.endpoint is Endpoint.USERS:
            body = [{"user_id": "u0001", "display_name": "Manager 01"}]
        elif req.endpoint is Endpoint.STATS_WEEK:
            body = {"p1": {"pass_yd": 300 + (req.week or 0)}}
        _write(directory, req, body)
    _write(directory, players_request(), {"p1": {"player_id": "p1", "position": "QB"}})
    return directory


EXPECTED_REQUESTS = (
    1  # state
    + 3  # league x3 (lineage)
    + 1  # drafts
    + 2  # draft + picks
    + 5  # users, rosters, traded_picks, brackets x2
    + 3 * WEEKS  # matchups, transactions, stats
    + 1  # players
)


@dataclass
class Env:
    engine: Engine
    store: RawStore
    fixtures: Path
    db: Path


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    db = tmp_path / "capture.db"
    engine = create_app_engine(f"sqlite:///{db}")
    upgrade_to_head(engine=engine)
    yield Env(engine, RawStore(tmp_path / "raw"), write_chain(tmp_path / "fx"), db)
    engine.dispose()


def _capture(
    env: Env,
    *,
    at: datetime = T0,
    source: Any = None,
    dry_run: bool = False,
    season: int | None = None,
) -> Any:
    src = source or FixtureSleeperLeagueSource(env.fixtures, fetched_at=at)
    with Session(env.engine) as ss:
        summary = capture_season(
            ss,
            src,
            env.store,
            start_league_id=L28,
            season=season,
            run_id=f"run-{at:%H%M%S}",
            now=at,
            dry_run=dry_run,
        )
        ss.commit()
    return summary


def _rows(env: Env) -> int:
    with Session(env.engine) as ss:
        return int(ss.scalar(select(func.count()).select_from(SourcePayload)) or 0)


def _files(env: Env) -> int:
    return len(list(env.store.root.rglob("*.json.gz")))


def test_capture_archives_every_endpoint(env: Env) -> None:
    s = _capture(env)
    assert (s.season, s.league_id, s.max_week) == (2028, L28, WEEKS)
    assert [link.season for link in s.lineage] == [2028, 2027, 2026]
    assert s.requests == EXPECTED_REQUESTS
    assert s.new_revisions == EXPECTED_REQUESTS
    assert s.errors == []
    assert _rows(env) == EXPECTED_REQUESTS
    with Session(env.engine) as ss:
        endpoints = set(ss.scalars(select(SourcePayload.endpoint).distinct()))
        lineage_seasons = sorted(
            ss.scalars(select(SourcePayload.season_year).where(SourcePayload.endpoint == "league"))
        )
    # user_leagues is fetched only to discover a renewed league (S9-D4)
    assert endpoints == {e.value for e in Endpoint} - {Endpoint.USER_LEAGUES.value}
    assert lineage_seasons == [2026, 2027, 2028]
    # identical bodies (e.g. the many "[]" payloads) share one file
    assert _files(env) < EXPECTED_REQUESTS


def test_rerun_adds_no_files_or_rows(env: Env) -> None:
    _capture(env)
    rows, files = _rows(env), _files(env)
    s = _capture(env, at=T0 + timedelta(hours=1))
    assert s.players_skipped  # players directory is fresh (24 h window)
    assert s.new_revisions == 0
    assert s.new_files == 0
    assert s.unchanged == EXPECTED_REQUESTS - 1
    assert (_rows(env), _files(env)) == (rows, files)
    with Session(env.engine) as ss:
        counts = set(ss.scalars(select(SourcePayload.fetch_count)))
    assert counts == {1, 2}  # players fetched once, everything else twice


def test_players_refetched_after_refresh_window(env: Env) -> None:
    _capture(env)
    s = _capture(env, at=T0 + timedelta(hours=25))
    assert not s.players_skipped


def test_changed_payload_is_a_new_revision_and_archive_replays(env: Env) -> None:
    _capture(env)
    week2 = next(
        r
        for r in league_requests(L28, 2028, WEEKS)
        if r.endpoint is Endpoint.MATCHUPS and r.week == 2
    )
    _write(env.fixtures, week2, [{"roster_id": 1, "matchup_id": 1, "points": 999.0}])
    later = T0 + timedelta(days=2)
    s = _capture(env, at=later)
    assert s.new_revisions == 1
    with Session(env.engine) as ss:
        newest = ArchiveSleeperLeagueSource(ss, env.store).get(week2)
        before = ArchiveSleeperLeagueSource(ss, env.store, as_of=T0 + timedelta(days=1)).get(week2)
        assert newest.json()[0]["points"] == 999.0
        assert before.json()[0]["points"] == 102.5
        with pytest.raises(ArchiveMissError):
            ArchiveSleeperLeagueSource(ss, env.store).get(league_request("nope"))


def test_archive_replay_equals_the_live_capture(env: Env) -> None:
    _capture(env)
    with Session(env.engine) as ss:
        archive = ArchiveSleeperLeagueSource(ss, env.store)
        fixture = FixtureSleeperLeagueSource(env.fixtures)
        for req in league_requests(L28, 2028, WEEKS):
            assert archive.get(req).body == fixture.get(req).body


@dataclass
class _Flaky:
    inner: FixtureSleeperLeagueSource
    fail_url: str

    def get(self, request: PayloadRequest) -> FetchedPayload:
        if request.url == self.fail_url:
            raise TransientHTTPError(f"GET {request.url} failed after retries")
        return self.inner.get(request)


def test_one_failed_request_is_reported_and_the_rest_archived(env: Env) -> None:
    bad = f"https://api.sleeper.app/v1/league/{L28}/traded_picks"
    s = _capture(env, source=_Flaky(FixtureSleeperLeagueSource(env.fixtures, T0), bad))
    assert len(s.errors) == 1
    assert bad in s.errors[0]
    assert _rows(env) == EXPECTED_REQUESTS - 1


def test_dry_run_writes_nothing(env: Env) -> None:
    s = _capture(env, dry_run=True)
    assert s.requests == EXPECTED_REQUESTS
    assert (_rows(env), _files(env)) == (0, 0)


def test_capture_on_a_sealed_db_leaves_sealed_data_unchanged(tmp_path: Path) -> None:
    db = build_sealed_db(tmp_path)
    before = _all_fingerprints(db)
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        e = Env(engine, RawStore(tmp_path / "raw"), write_chain(tmp_path / "fx"), db)
        s = _capture(e)
        assert s.new_revisions == EXPECTED_REQUESTS
    finally:
        engine.dispose()
    assert _all_fingerprints(db) == before
    conn = connect(str(db))
    try:
        assert verify(conn, full=True).ok
    finally:
        conn.close()


def test_s2_migration_round_trip_on_a_sealed_db(tmp_path: Path) -> None:
    db = build_sealed_db(tmp_path)
    before = _all_fingerprints(db)

    def tables() -> set[str]:
        c = sqlite3.connect(db)
        try:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()

    def verified() -> bool:
        conn = connect(str(db))
        try:
            return verify(conn, full=True).ok
        finally:
            conn.close()

    assert "source_payloads" in tables()
    engine = create_app_engine(f"sqlite:///{db}")
    try:
        cfg = _make_config()
        with engine.connect() as connection, _fk_disabled_for_sqlite(connection):
            cfg.attributes["connection"] = connection
            command.downgrade(cfg, "e2a4c6e8f0b1")
        assert "source_payloads" not in tables()
        assert _all_fingerprints(db) == before
        assert verified()
        upgrade_to_head(engine=engine)
    finally:
        engine.dispose()
    assert "source_payloads" in tables()
    assert _all_fingerprints(db) == before
    assert verified()


# ----- real-shaped, pseudonymized fixtures (built from the 2026 capture) -----

REAL_FX = Path(__file__).resolve().parents[1] / "fixtures" / "sleeper_league" / "season_2026"
FAKE_LEAGUE = "900000000000000001"


def test_capture_replays_the_pseudonymized_2026_fixtures(tmp_path: Path) -> None:
    db = tmp_path / "real.db"
    engine = create_app_engine(f"sqlite:///{db}")
    upgrade_to_head(engine=engine)
    try:
        with Session(engine) as ss:
            s = capture_season(
                ss,
                FixtureSleeperLeagueSource(REAL_FX),
                RawStore(tmp_path / "raw"),
                start_league_id=FAKE_LEAGUE,
                season=2026,
                run_id="fx",
                now=T0,
            )
            ss.commit()
    finally:
        engine.dispose()
    assert (s.season, s.max_week, s.errors) == (2026, 9, [])
    assert s.requests == 1 + 1 + 1 + 2 + 5 + 3 * 9 + 1


def test_players_points_equal_stats_dot_scoring_in_fixtures() -> None:
    """The S2 probe's key finding, pinned: Sleeper points = Σ stat x scoring value."""
    league = json.loads((REAL_FX / f"v1_league_{FAKE_LEAGUE}.json").read_text())
    scoring = league["scoring_settings"]
    checked = 0
    for week in range(1, 10):
        matchups = json.loads(
            (REAL_FX / f"v1_league_{FAKE_LEAGUE}_matchups_{week}.json").read_text()
        )
        stats_rows = json.loads(
            (REAL_FX / f"stats_nfl_2026_{week}_season_type_regular.json").read_text()
        )
        stats = {r["player_id"]: r["stats"] for r in stats_rows}
        for m in matchups:
            for pid, pts in m["players_points"].items():
                calc = sum(v * scoring.get(k, 0) for k, v in stats.get(pid, {}).items())
                assert round(calc, 2) == pytest.approx(pts, abs=0.011), (week, pid)
                checked += 1
    assert checked > 1000


L29 = "900000000000002029"


def _renewal_fixtures(env: Env, *, previous: str, is_owner: bool = True) -> None:
    """State says 2029; the 2028 commissioner's 2029 leagues list one candidate."""
    from ff_pipeline.crawlers.sleeper_league.endpoints import (
        drafts_request,
        league_request,
        user_leagues_request,
        users_request,
    )

    fx = env.fixtures
    (fx / "v1_state_nfl.json").write_text(
        json.dumps({"season": "2029", "season_type": "pre", "week": 0, "leg": 0})
    )
    users = [{"user_id": "u0001", "display_name": "Manager 01", "is_owner": is_owner or None}]
    _write(fx, users_request(L28, 2028), users)
    _write(
        fx,
        user_leagues_request("u0001", 2029),
        [
            {"league_id": "900000000000009999", "previous_league_id": "0", "season": "2029"},
            {"league_id": L29, "previous_league_id": previous, "season": "2029"},
        ],
    )
    _write(
        fx,
        league_request(L29),
        {
            "league_id": L29,
            "season": "2029",
            "previous_league_id": L28,
            "status": "pre_draft",
            "name": "Fixture League",
            "draft_id": None,
        },
    )
    _write(fx, drafts_request(L29, 2029), [])
    for req in league_requests(L29, 2029, 0):
        _write(fx, req, [])


def test_rollover_discovers_and_archives_the_renewed_league(env: Env) -> None:
    _renewal_fixtures(env, previous=L28)
    s = _capture(env)
    assert s.renewed_league_id == L29
    assert (s.season, s.league_id) == (2029, L29)
    assert [link.season for link in s.lineage] == [2029, 2028, 2027, 2026]
    assert s.max_week == 0  # pre-draft: no weeks yet
    assert s.errors == []


def test_rollover_needs_the_right_predecessor_and_a_commissioner(env: Env) -> None:
    from ff_pipeline.crawlers.sleeper_league.capture import CaptureSummary, _Archiver, find_renewal

    for previous, owner, want in (
        (L28, True, L29),
        ("900000000000000123", True, None),
        (L28, False, None),
    ):
        _renewal_fixtures(env, previous=previous, is_owner=owner)
        with Session(env.engine) as ss:
            arc = _Archiver(
                ss,
                FixtureSleeperLeagueSource(env.fixtures, fetched_at=T0),
                env.store,
                "t",
                True,
                CaptureSummary(dry_run=True),
            )
            assert find_renewal(arc, L28, 2028, 2029) == want
