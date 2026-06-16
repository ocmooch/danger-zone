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

import re
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


# Minimal HTML the parser accepts that signals end-of-pagination so the
# sweep terminates after two real-fixture pages. ``total_count=1`` plus a
# single row drives the sweep's "manual advance" fallback to a stop on
# the very next iteration (current_offset + len(rows) >= total_count).
_AVAILABILITY_TERMINATOR = (
    '<html><body><span class="paginationTitle">1 - 1 of 1</span>'
    '<table class="tableType-player"><tbody>'
    '<tr><td class="playerNameAndInfo">'
    '<a class="playerName" href="/players/card?playerId=999999">Sentinel</a>'
    "<em>WR - FA</em></td>"
    '<td class="playerOwner">FA</td></tr>'
    "</tbody></table></body></html>"
)


class _StubFetcher:
    """Maps URL substrings to fixture file contents."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._players_calls = 0

    def get_html(self, url: str) -> str:
        self.calls.append(url)
        if "/owners" in url:
            return _load("owners.html")
        if "/team/" in url:
            # Every team URL returns the same real roster fixture, but with
            # its NFL.com player ids offset per team so each of the 12 teams
            # carries a *distinct* set of players. The season-scoped
            # UNIQUE(season_year, week, player_id) enforces one team per
            # player per week, so reusing identical players across teams
            # would collapse all 12 rosters onto one team. The offset is
            # keyed on the URL's teamId (not a call counter) so re-runs
            # resolve to the same players and stay idempotent. Distinct ids
            # also defeat the resolver's fuzzy name merge: an existing
            # player's conflicting nfl_com_player_id rejects the match.
            return _offset_player_ids(_load("team_roster_1.html"), url)
        if "schedule" in url:
            return _load("weekly_matchups_w7.html")
        if "transactions" in url:
            return _load("transactions.html")
        if "/players?" in url:
            # Real availability fixtures each advertise next_offset=26
            # (NFL.com's pagination is 1-indexed, off-by-one from PAGE_SIZE).
            # We serve page_0 first, page_25 second, then a terminator so
            # the sweep stops at exactly 2 real-fixture pages plus 1 empty.
            self._players_calls += 1
            if self._players_calls == 1:
                return _load("availability_page_0.html")
            if self._players_calls == 2:
                return _load("availability_page_25.html")
            return _AVAILABILITY_TERMINATOR
        # Default: league home
        return _load("league_home.html")


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# Matches both the href form (``playerId=NNN``) the parser reads first and
# the class fallback (``playerNameId-NNN``); offsetting both keeps a single
# player's two id encodings in agreement.
_PLAYER_ID_TOKEN = re.compile(r"(playerId=|playerNameId-)(\d+)")
_TEAM_ID_IN_URL = re.compile(r"/team/(\d+)")


def _offset_player_ids(html: str, url: str) -> str:
    """Make a reused roster fixture team-unique by offsetting player ids.

    The offset derives from the URL's teamId so the same team always maps
    to the same players (idempotent re-runs); base ids are ~2.5M, so a
    100M-per-team stride never collides across the 12 teams.
    """
    m = _TEAM_ID_IN_URL.search(url)
    team_no = int(m.group(1)) if m else 0
    offset = team_no * 100_000_000
    return _PLAYER_ID_TOKEN.sub(lambda mm: f"{mm.group(1)}{int(mm.group(2)) + offset}", html)


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
    # Real Week 17 schedule fixture: 6 head-to-head pairings -> 12 rows
    # (one row per team-side, mirroring matchups schema).
    assert len(matchups) == 12

    # All matchup team_ids should resolve to internal team rows whose
    # team_abbrev is the NFL.com team id 1..12.
    by_team_name = {m.team_id: session.get(Team, m.team_id) for m in matchups}
    nfl_team_abbrevs = {t.team_abbrev for t in by_team_name.values() if t is not None}
    assert nfl_team_abbrevs == {str(i) for i in range(1, 13)}


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
    # Real fixture has 4 drops + 4 adds + 16 lineup moves = 24 rows; the
    # full league diary now captures lineup changes too. The sweep also
    # de-dups the boundary overlap (the fixture's "next" link points back at
    # itself), so the second page adds nothing.
    assert after_first == 24
    types = [t.transaction_type for t in session.execute(select(Transaction)).scalars().all()]
    assert types.count("lineup_change") == 16
    assert types.count("drop") == 4
    assert types.count("free_agent_add") == 4

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
    # Real fixtures: 25 players per page x 2 pages = 50 unique availability
    # rows (the stub serves a terminator on the third call so the sweep
    # stops cleanly without ingesting the sentinel row).
    assert len(avail) == 50


@pytest.mark.integration
def test_runner_audit_snapshot_does_not_overwrite_pre_kickoff(session: Session) -> None:
    """Mid-week audit run should write availability rows with
    ``is_pre_kickoff_snapshot=False`` so the canonical pre-kickoff row
    survives unmolested."""

    # Two distinct fetcher instances so each run gets a fresh availability
    # call counter — otherwise the second run would see the sweep
    # terminator on its very first /players? call.
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=_StubFetcher(),
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=_StubFetcher(),
        snapshot_kind="audit",
    )
    session.commit()

    avail = session.execute(select(PlayerAvailability)).scalars().all()
    pre = [a for a in avail if a.is_pre_kickoff_snapshot]
    audit = [a for a in avail if not a.is_pre_kickoff_snapshot]
    # 50 unique players in pre-kickoff + 50 mirrored in audit (the two
    # rows have different is_pre_kickoff_snapshot values, so they don't
    # collide on the player+season+week unique key).
    assert len(pre) == 50
    assert len(audit) == 50


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
    # Each of the 12 teams carries the 16-entry roster fixture with its
    # player ids offset per team (see _offset_player_ids), so the roster
    # contributes 16 * 12 = 192 distinct nfl_com_player_ids. The offset
    # stride (100M) is far above any real base id, so the offset roster
    # ids are exactly the ids at/above 100M.
    roster_ids = {i for i in nfl_ids if i.isdigit() and int(i) >= 100_000_000}
    assert len(roster_ids) == 192
    # Joe Burrow (base id 2563722) on team 1 → offset by 100M.
    assert str(100_000_000 + 2563722) in nfl_ids
    # Availability fixtures (real Week 17 captures): Mahomes is the top
    # row of page 0, Boutte is the top of page 25 — both are FA in the
    # live league but still need a Player stub. These come from the
    # /players sweep, not the /team pages, so they are not offset.
    assert "2558125" in nfl_ids  # Patrick Mahomes (availability page 0)
    assert "2570092" in nfl_ids  # Kayshon Boutte (availability page 25)


# ---------------------------------------------------------------------------
# Snapshot-kind heuristic
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_runner_audit_does_not_overwrite_authoritative_roster(session: Session) -> None:
    """An audit sync must not clobber an existing authoritative roster.

    The live NFL.com roster page is not week-aware (always returns today's
    roster), so an audit run pointed at a week that already holds a
    week-accurate snapshot (pre_kickoff / draft / history) must quarantine the
    roster write rather than stamp the current rosters onto that week — the
    2025/2026 week-1 corruption. The authoritative rows must survive byte-for
    -byte and the run must surface a warning.
    """
    run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=_StubFetcher(),
        snapshot_kind="pre_kickoff",
    )
    session.commit()

    before = {
        (r.team_id, r.player_id): (r.roster_slot, (r.extra_data or {}).get("snapshot_kind"))
        for r in session.execute(select(TeamRoster)).scalars().all()
    }
    assert before  # sanity: the pre_kickoff run wrote rosters
    assert all(kind == "pre_kickoff" for _, kind in before.values())

    result = run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=_StubFetcher(),
        snapshot_kind="audit",
    )
    session.commit()

    after = {
        (r.team_id, r.player_id): (r.roster_slot, (r.extra_data or {}).get("snapshot_kind"))
        for r in session.execute(select(TeamRoster)).scalars().all()
    }
    # Roster write was quarantined: the authoritative snapshot is untouched and
    # no audit row was written.
    assert after == before
    assert result.rosters_added == 0
    assert result.rosters_updated == 0
    assert any("audit roster write skipped" in w for w in result.warnings)


@pytest.mark.integration
def test_runner_audit_seeds_an_empty_week(session: Session) -> None:
    """The quarantine is precedence-only: an audit sync still writes rosters
    when the target week has no authoritative snapshot yet (otherwise an audit
    run could never seed an in-progress week's first roster)."""
    result = run_nfl_com(
        session,
        league_id="36271",
        year=2025,
        week=7,
        fetcher=_StubFetcher(),
        snapshot_kind="audit",
    )
    session.commit()

    rosters = session.execute(select(TeamRoster)).scalars().all()
    assert len(rosters) == 192
    assert all((r.extra_data or {}).get("snapshot_kind") == "audit" for r in rosters)
    assert result.rosters_added == 192
    assert not any("audit roster write skipped" in w for w in result.warnings)


def test_default_snapshot_kind_sunday_morning_is_pre_kickoff() -> None:
    sunday_noon_utc = datetime(2025, 10, 19, 16, 30, tzinfo=UTC)  # 12:30pm ET
    assert _default_snapshot_kind(sunday_noon_utc) == "pre_kickoff"


def test_default_snapshot_kind_sunday_evening_is_audit() -> None:
    sunday_evening_utc = datetime(2025, 10, 19, 23, 0, tzinfo=UTC)  # 7pm ET
    assert _default_snapshot_kind(sunday_evening_utc) == "audit"


def test_default_snapshot_kind_tuesday_is_audit() -> None:
    tuesday_utc = datetime(2025, 10, 21, 9, 0, tzinfo=UTC)
    assert _default_snapshot_kind(tuesday_utc) == "audit"
