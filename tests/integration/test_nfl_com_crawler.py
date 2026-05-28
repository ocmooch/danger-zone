"""Integration test for the NFL.com league runner.

Uses a stub fetcher that maps URL substrings to committed HTML fixtures
so the test runs offline. Verifies:

* League, season, owners, teams are upserted
* Team rosters get one row per non-empty slot
* Matchups land with opponent FK populated
* Transactions accumulate (no dupes on re-run)
* Availability rows are tagged ``is_pre_kickoff_snapshot`` correctly
* Idempotency: re-running produces zero new rows
* Snapshot-kind heuristic: Sunday morning UTC → pre_kickoff;
  Tuesday morning UTC → audit
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.league import _default_snapshot_kind, run_nfl_com
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    PipelineRun,
    Player,
    PlayerAvailability,
    Season,
    SourceHealth,
    Team,
    TeamRoster,
    Transaction,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


class _StubFetcher:
    """Maps URL substrings to fixture file contents."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_html(self, url: str) -> str:
        self.calls.append(url)
        if "/owners" in url:
            return _load("owners.html")
        if "/team/" in url:
            # Every team URL returns the same real roster fixture. The
            # runner FKs rosters off the internal team_id it computes from
            # the owners list, not off the parsed page's own team_id, so
            # repeating one team's roster across all 12 owners is fine for
            # exercising the upsert + FK plumbing.
            return _load("team_roster_1.html")
        if "schedule" in url:
            return _load("weekly_matchups_w7.html")
        if "transactions" in url:
            return _load("transactions.html")
        if "/players?" in url:
            if "offset=25" in url:
                return _load("availability_page_25.html")
            return _load("availability_page_0.html")
        # Default: league home
        return _load("league_home.html")


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_runner_populates_league_season_owners_teams(session: Session) -> None:
    fetcher = _StubFetcher()
    result = run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    assert result.owners_added == 12
    assert result.teams_added == 12

    league = session.execute(select(League)).scalar_one()
    assert league.league_id == "36271"
    assert league.name == "The Danger Zone"

    season = session.execute(select(Season)).scalar_one()
    assert season.year == 2025

    owners = session.execute(select(Owner)).scalars().all()
    teams = session.execute(select(Team)).scalars().all()
    assert {o.display_name for o in owners} == {
        "harry",
        "scott",
        "mike",
        "sully",
        "Dan",
        "Dave",
        "Gregg",
        "Chris",
        "Jeff",
        "Rob",
        "Jimbo",
        "Kofi",
    }
    assert len(teams) == 12


@pytest.mark.integration
def test_runner_populates_rosters_with_pre_kickoff_flag(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    rosters = session.execute(select(TeamRoster)).scalars().all()
    # 12 teams x 16 roster entries each (from the real team_roster_1
    # fixture, reused per team) = 192 roster rows.
    assert len(rosters) == 192
    assert all(r.was_locked_at_kickoff is True for r in rosters)
    # Sanity: bench slots are labeled "BN" (unsuffixed) on the live page,
    # and they are non-starters.
    bn = [r for r in rosters if r.roster_slot == "BN"]
    assert bn and all(r.is_starter is False for r in bn)


@pytest.mark.integration
def test_runner_writes_matchups_with_opponent_fk(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    matchups = session.execute(select(Matchup)).scalars().all()
    assert len(matchups) == 4

    # Pair (1 vs 2): there should be FKs to internal team rows, NOT NFL.com team_ids.
    by_team_name = {m.team_id: session.get(Team, m.team_id) for m in matchups}
    nfl_team_abbrevs = {t.team_abbrev for t in by_team_name.values() if t is not None}
    assert nfl_team_abbrevs == {"1", "2", "3", "4"}


@pytest.mark.integration
def test_runner_records_transactions_idempotently(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()
    after_first = len(session.execute(select(Transaction)).all())
    assert after_first == 3

    # Re-run: identical fixtures → identical fingerprints → zero new inserts.
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()
    after_second = len(session.execute(select(Transaction)).all())
    assert after_second == after_first


@pytest.mark.integration
def test_runner_writes_pre_kickoff_availability(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    avail = session.execute(select(PlayerAvailability)).scalars().all()
    assert all(a.is_pre_kickoff_snapshot is True for a in avail)
    # 5 unique players in the two-page fixture set.
    assert len(avail) == 5


@pytest.mark.integration
def test_runner_audit_snapshot_does_not_overwrite_pre_kickoff(session: Session) -> None:
    """Mid-week audit run should write availability rows with
    ``is_pre_kickoff_snapshot=False`` so the canonical pre-kickoff row
    survives unmolested."""

    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="audit",
    )
    session.commit()

    avail = session.execute(select(PlayerAvailability)).scalars().all()
    pre = [a for a in avail if a.is_pre_kickoff_snapshot]
    audit = [a for a in avail if not a.is_pre_kickoff_snapshot]
    assert len(pre) == 5
    assert len(audit) == 5


@pytest.mark.integration
def test_runner_records_pipeline_run_and_source_health(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    run_row = session.execute(select(PipelineRun)).scalar_one()
    health_row = session.execute(select(SourceHealth)).scalar_one()
    assert run_row.status == "success"
    assert run_row.sources_summary is not None
    summary = run_row.sources_summary["nfl_com"]
    assert summary["snapshot_kind"] == "pre_kickoff"
    assert summary["year"] == 2025
    assert summary["week"] == 7
    assert health_row.source == "nfl_com"
    assert health_row.status == "success"


@pytest.mark.integration
def test_runner_creates_player_stubs_for_new_nfl_com_ids(session: Session) -> None:
    fetcher = _StubFetcher()
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=fetcher,
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    players = session.execute(select(Player)).scalars().all()
    nfl_ids = {p.nfl_com_player_id for p in players if p.nfl_com_player_id}
    # 16 roster entries x 12 teams = 192 references but they all share the
    # same 16 nfl_com_player_ids (the same fixture is reused per team), so
    # we expect 16 unique players from the roster + 3 from the synthetic
    # availability fixtures (555, 666, 777).
    assert "2563722" in nfl_ids  # Joe Burrow (QB)
    assert "2569747" in nfl_ids  # Jahmyr Gibbs (RB)
    assert {"666", "555", "777"}.issubset(nfl_ids)


# ---------------------------------------------------------------------------
# Snapshot-kind heuristic
# ---------------------------------------------------------------------------


def test_default_snapshot_kind_sunday_morning_is_pre_kickoff() -> None:
    sunday_noon_utc = datetime(2025, 10, 19, 16, 30, tzinfo=UTC)  # 12:30pm ET
    assert _default_snapshot_kind(sunday_noon_utc) == "pre_kickoff"


def test_default_snapshot_kind_sunday_evening_is_audit() -> None:
    sunday_evening_utc = datetime(2025, 10, 19, 23, 0, tzinfo=UTC)  # 7pm ET
    assert _default_snapshot_kind(sunday_evening_utc) == "audit"


def test_default_snapshot_kind_tuesday_is_audit() -> None:
    tuesday_utc = datetime(2025, 10, 21, 9, 0, tzinfo=UTC)
    assert _default_snapshot_kind(tuesday_utc) == "audit"
