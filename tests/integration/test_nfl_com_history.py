"""Integration tests for the NFL.com history reconstruction.

Covers the two pieces of logic that the parser unit tests can't reach:

* :func:`reconstruct_standings` mapping the parsed finish order onto the
  DB — including overwriting the (wrong, current-era) team names the
  earlier backfill stamped, and deriving the regular-season-week boundary
  from the champion's game count.
* :func:`derive_team_records` counting only regular-season weeks once
  :func:`reconstruct_matchups` has classified playoff weeks by that
  boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.history import (
    derive_team_records,
    reconstruct_draft,
    reconstruct_lineups,
    reconstruct_matchups,
    reconstruct_owners,
    reconstruct_standings,
)
from ff_pipeline.crawlers.nfl_com.league import _upsert_owners_and_teams
from ff_pipeline.crawlers.nfl_com.parsers import ParsedOwner
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    Player,
    Season,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.owner_identities import seed_owner_identity_override

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class _StandingsStub:
    def get_html(self, url: str) -> str:
        assert "standings" in url
        return _load("standings_2024.html")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed_league_and_teams(session: Session, *, year: int = 2024, n_teams: int = 12) -> int:
    """Seed a league/season/owners/teams the way the earlier backfill did.

    Team names are deliberately *wrong* (current-era placeholders) so the
    standings reconstruction has something to correct. ``team_abbrev``
    holds the NFL.com team id (1..n).
    """
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=year, status="in_progress")
    session.add(season)
    session.flush()
    for nfl_team_id in range(1, n_teams + 1):
        owner = Owner(league_id="36271", display_name=f"owner{nfl_team_id}", is_active=True)
        session.add(owner)
        session.flush()
        session.add(
            Team(
                season_id=season.season_id,
                owner_id=owner.owner_id,
                team_name=f"PLACEHOLDER NAME {nfl_team_id}",
                team_abbrev=str(nfl_team_id),
            )
        )
    session.flush()
    return season.season_id


@pytest.mark.integration
def test_reconstruct_standings_sets_finish_order_and_fixes_names(session: Session) -> None:
    season_id = _seed_league_and_teams(session)
    outcome = reconstruct_standings(session, league_id="36271", year=2024, fetcher=_StandingsStub())
    session.commit()

    assert outcome.teams_ranked == 12

    season = session.get(Season, season_id)
    assert season is not None
    assert season.status == "completed"
    # Champion's record on the standings fixture is 8-6-0 → 14 reg weeks.
    assert season.regular_season_weeks == 14

    # NFL team id 6 is the 2024 champion "Putting the CAP in CHAMP".
    nfl6 = session.execute(
        select(Team).where(Team.season_id == season_id, Team.team_abbrev == "6")
    ).scalar_one()
    assert season.champion_team_id == nfl6.team_id
    assert nfl6.final_rank == 1
    assert nfl6.playoff_finish == 1
    # The placeholder name must have been replaced with the real per-season one.
    assert nfl6.team_name == "Putting the CAP in CHAMP"
    assert (nfl6.regular_season_wins, nfl6.regular_season_losses) == (8, 6)
    assert nfl6.regular_season_points_for == pytest.approx(1765.40)

    # Last place (12th) is NFL team id 11 on the fixture.
    nfl11 = session.execute(
        select(Team).where(Team.season_id == season_id, Team.team_abbrev == "11")
    ).scalar_one()
    assert season.last_place_team_id == nfl11.team_id
    assert nfl11.final_rank == 12


@pytest.mark.integration
def test_derive_team_records_counts_regular_season_only(session: Session) -> None:
    season_id = _seed_league_and_teams(session, n_teams=2)
    season = session.get(Season, season_id)
    assert season is not None
    season.regular_season_weeks = 2
    season.playoff_weeks = 1
    team_a, team_b = (
        session.execute(select(Team).where(Team.season_id == season_id)).scalars().all()
    )

    # Two regular-season weeks: A wins both. One playoff week: B wins —
    # must NOT count toward A's regular-season record.
    def _pair(week: int, is_playoff: bool, a_score: float, b_score: float) -> list[Matchup]:
        return [
            Matchup(
                season_id=season_id,
                week=week,
                team_id=team_a.team_id,
                opponent_team_id=team_b.team_id,
                team_score=a_score,
                opponent_score=b_score,
                is_win=a_score > b_score,
                is_playoff=is_playoff,
                is_consolation=False,
            ),
            Matchup(
                season_id=season_id,
                week=week,
                team_id=team_b.team_id,
                opponent_team_id=team_a.team_id,
                team_score=b_score,
                opponent_score=a_score,
                is_win=b_score > a_score,
                is_playoff=is_playoff,
                is_consolation=False,
            ),
        ]

    for m in (
        *_pair(1, False, 100.0, 90.0),
        *_pair(2, False, 110.0, 95.0),
        *_pair(3, True, 50.0, 120.0),  # playoff — excluded
    ):
        session.add(m)
    session.flush()

    updated = derive_team_records(session, league_id="36271", year=2024)
    session.commit()
    assert updated == 2

    session.refresh(team_a)
    session.refresh(team_b)
    # A: 2-0 regular season, 210 PF / 185 PA (playoff week excluded).
    assert (team_a.regular_season_wins, team_a.regular_season_losses) == (2, 0)
    assert team_a.regular_season_points_for == pytest.approx(210.0)
    assert team_a.regular_season_points_against == pytest.approx(185.0)
    assert (team_b.regular_season_wins, team_b.regular_season_losses) == (0, 2)


def _schedule_html(team_a: int, team_b: int) -> str:
    return f"""
    <ul>
      <li class="matchup">
        <div class="teamWrap">
          <a class="teamName" href="/league/36271/history/2024/teamhome?teamId={team_a}">Team {team_a}</a>
          <span class="teamTotal">100.0</span>
        </div>
        <div class="teamWrap">
          <a class="teamName" href="/league/36271/history/2024/teamhome?teamId={team_b}">Team {team_b}</a>
          <span class="teamTotal">90.0</span>
        </div>
      </li>
    </ul>
    """


class _MatchupsWithBracketStub:
    def get_html(self, url: str) -> str:
        if "playoffs" in url:
            return _load("league_home.html")
        if "scheduleDetail=15" in url:
            return _schedule_html(1, 12)
        if "scheduleDetail=16" in url:
            return _schedule_html(2, 3)
        raise AssertionError(url)


@pytest.mark.integration
def test_reconstruct_matchups_marks_postseason_consolation_from_bracket(
    session: Session,
) -> None:
    season_id = _seed_league_and_teams(session)
    season = session.get(Season, season_id)
    assert season is not None
    season.regular_season_weeks = 14

    outcome = reconstruct_matchups(
        session,
        league_id="36271",
        year=2024,
        fetcher=_MatchupsWithBracketStub(),
        weeks=(15, 16),
    )
    session.commit()

    assert outcome.playoff_weeks == (15, 16)
    rows = (
        session.execute(
            select(Matchup)
            .where(Matchup.season_id == season_id)
            .order_by(Matchup.week, Matchup.team_id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4
    by_week = {week: [m.is_consolation for m in rows if m.week == week] for week in (15, 16)}
    # NFL team ids 1 and 12 are in the captured championship bracket.
    assert by_week[15] == [False, False]
    # NFL team ids 2 and 3 are not, so their postseason game is consolation.
    assert by_week[16] == [True, True]


class _DraftStub:
    """Serve the base (round 1) and round-2 draft fixtures; rounds 3..15
    return an empty page so the runner stops adding picks past the two
    rounds we have real markup for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_html(self, url: str) -> str:
        import re

        self.calls.append(url)
        assert "draftresults" in url
        detail = re.search(r"draftResultsDetail=(\d+)", url)
        if detail is None:
            return _load("draftresults_2024_round1.html")
        if int(detail.group(1)) == 2:
            return _load("draftresults_2024_round2.html")
        return "<html><body></body></html>"


def _minimal_gamecenter_html() -> str:
    def side(team_id: int, player_id: int, player_name: str) -> str:
        return f"""
        <div class="teamWrap teamWrap-{team_id}">
          <a class="teamName" href="/league/36271/history/2024/teamhome?teamId={team_id}">Team {team_id}</a>
          <span class="teamTotal">10.0</span>
          <table class="tableType-player"><tbody>
            <tr>
              <td class="teamPosition">QB</td>
              <td><a class="playerName" href="/players/card?playerId={player_id}">{player_name}</a><em>QB - BUF</em></td>
              <td><span class="playerTotal">10.0</span></td>
            </tr>
          </tbody></table>
        </div>
        """

    return f"<html><body>{side(1, 1001, 'Week One A')}{side(2, 1002, 'Week One B')}</body></html>"


class _LineupsStub:
    def get_html(self, url: str) -> str:
        assert "teamgamecenter" in url
        return _minimal_gamecenter_html()


@pytest.mark.integration
def test_reconstruct_lineups_clears_stale_week_snapshot_before_writing(
    session: Session,
) -> None:
    season_id = _seed_league_and_teams(session, n_teams=3)
    stale = Player(name_full="Modern Placeholder", nfl_com_player_id="9001")
    session.add(stale)
    session.flush()
    orphan_team = session.execute(
        select(Team).where(Team.season_id == season_id, Team.team_abbrev == "3")
    ).scalar_one()
    session.add(
        TeamRoster(
            team_id=orphan_team.team_id,
            player_id=stale.player_id,
            season_year=2024,
            week=1,
            roster_slot="QB",
            is_starter=True,
            was_locked_at_kickoff=False,
            extra_data={"snapshot_kind": "audit"},
        )
    )
    session.flush()

    outcome = reconstruct_lineups(
        session,
        league_id="36271",
        year=2024,
        fetcher=_LineupsStub(),
        weeks=[1],
    )
    session.commit()

    assert outcome.fetch_failures == 0
    rows = (
        session.execute(
            select(TeamRoster).where(TeamRoster.season_year == 2024, TeamRoster.week == 1)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert orphan_team.team_id not in {row.team_id for row in rows}
    assert stale.player_id not in {row.player_id for row in rows}
    assert {(row.extra_data or {}).get("snapshot_kind") for row in rows} == {"history"}


@pytest.mark.integration
def test_reconstruct_draft_writes_ordered_picks(session: Session) -> None:
    season_id = _seed_league_and_teams(session)
    outcome = reconstruct_draft(session, league_id="36271", year=2024, fetcher=_DraftStub())
    session.commit()

    assert outcome.available is True
    assert outcome.picks_parsed == 24  # two rounds of 12
    assert outcome.txns_added == 24
    assert outcome.unknown_team_picks == 0

    txns = (
        session.execute(
            select(Transaction)
            .where(Transaction.season_id == season_id, Transaction.transaction_type == "draft")
            .order_by(Transaction.executed_at)
        )
        .scalars()
        .all()
    )
    assert len(txns) == 24
    # Contract: draft picks are direction='add', effective_week=0, and the
    # executed_at order is the true overall pick order (pick 1 earliest).
    assert all(t.direction == "add" for t in txns)
    assert all(t.effective_week == 0 for t in txns)
    assert [t.executed_at for t in txns] == sorted(t.executed_at for t in txns)
    # First pick by executed_at must be overall pick 1 — Christian McCaffrey
    # to the team whose NFL.com id is 3 (team_abbrev "3").
    first_team = session.get(Team, txns[0].team_id)
    assert first_team is not None
    assert first_team.team_abbrev == "3"

    # Each pick is mirrored onto team_rosters at week 0 with the draft
    # provenance, and the explicit overall/round live in extra_data.
    roster = session.execute(select(TeamRoster).where(TeamRoster.week == 0)).scalars().all()
    assert len(roster) == 24
    assert all(r.acquisition_type == "draft" and r.acquisition_week == 0 for r in roster)
    overalls = sorted((r.extra_data or {}).get("draft_overall") for r in roster)
    assert overalls == list(range(1, 25))


@pytest.mark.integration
def test_reconstruct_draft_is_idempotent(session: Session) -> None:
    _seed_league_and_teams(session)
    stub = _DraftStub()
    reconstruct_draft(session, league_id="36271", year=2024, fetcher=stub)
    session.commit()

    again = reconstruct_draft(session, league_id="36271", year=2024, fetcher=stub)
    session.commit()
    assert again.txns_added == 0
    assert again.txns_skipped == 24

    total = (
        session.execute(select(Transaction).where(Transaction.transaction_type == "draft"))
        .scalars()
        .all()
    )
    assert len(total) == 24


@pytest.mark.integration
def test_reconstruct_draft_records_nothing_when_unobtainable(session: Session) -> None:
    _seed_league_and_teams(session)

    class _EmptyDraftStub:
        def get_html(self, url: str) -> str:
            assert "draftresults" in url
            return "<html><body><p>This draft has not occurred.</p></body></html>"

    outcome = reconstruct_draft(session, league_id="36271", year=2024, fetcher=_EmptyDraftStub())
    session.commit()

    assert outcome.available is False
    assert outcome.picks_parsed == 0
    assert (
        session.execute(select(Transaction).where(Transaction.transaction_type == "draft"))
        .scalars()
        .first()
        is None
    )


def _owners_html(managers: dict[int, tuple[str, str]]) -> str:
    """Build a minimal /history/{year}/owners page.

    ``managers`` maps NFL team id -> (userId, username), mirroring the real
    ``table.tableType-team`` markup parse_owners consumes.
    """
    rows = []
    for team_id, (user_id, name) in sorted(managers.items()):
        rows.append(
            f'<tr class="team-{team_id}">'
            f'<td class="teamImageAndName first">'
            f'<a class="teamName teamId-{team_id}" href="/league/36271/history/2024/teamhome?teamId={team_id}">Team {team_id}</a></td>'
            f'<td class="teamOwnerName"><ul><li class="first last">'
            f'<span class="userName userId-{user_id}">{name}</span></li></ul></td>'
            f'<td class="teamCoManagerName"></td></tr>'
        )
    return '<table class="tableType-team"><tbody>' + "".join(rows) + "</tbody></table>"


class _OwnersStub:
    """Returns per-year owners HTML; T1/T2 swap managers and T3 changes hands."""

    _BY_YEAR: ClassVar[dict[int, dict[int, tuple[str, str]]]] = {
        2023: {1: ("100", "bob"), 2: ("200", "alice"), 3: ("400", "dave")},
        2024: {1: ("200", "alice"), 2: ("100", "bob"), 3: ("300", "carol")},
    }

    def get_html(self, url: str) -> str:
        year = 2023 if "/2023/" in url else 2024
        return _owners_html(self._BY_YEAR[year])


def _seed_two_seasons_stamped_current(session: Session) -> dict[int, int]:
    """Seed 2023+2024 with the backfill artifact: every season shows the 2024
    owner, each owner carrying its 2024 NFL userId. Returns season_id by year."""
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    # 2024 (latest) managers, stamped onto BOTH seasons.
    stamped = {1: ("200", "alice"), 2: ("100", "bob"), 3: ("300", "carol")}
    owner_by_uid: dict[str, int] = {}
    for uid, name in dict(stamped.values()).items():
        owner = Owner(league_id="36271", display_name=name, nfl_user_id=uid, is_active=True)
        session.add(owner)
        session.flush()
        owner_by_uid[uid] = owner.owner_id
    season_ids: dict[int, int] = {}
    for year in (2023, 2024):
        season = Season(league_id="36271", year=year, status="completed")
        session.add(season)
        session.flush()
        season_ids[year] = season.season_id
        for nfl_team_id, (uid, _name) in stamped.items():
            session.add(
                Team(
                    season_id=season.season_id,
                    owner_id=owner_by_uid[uid],
                    team_name=f"Team {nfl_team_id}",
                    team_abbrev=str(nfl_team_id),
                )
            )
    session.flush()
    return season_ids


@pytest.mark.integration
def test_reconstruct_owners_identities_tenure_and_safe_repointing(session: Session) -> None:
    season_ids = _seed_two_seasons_stamped_current(session)
    outcome = reconstruct_owners(
        session, league_id="36271", fetcher=_OwnersStub(), start_year=2023, end_year=2024
    )
    session.commit()

    # alice/bob/carol (active) updated in place; dave added as a new identity.
    assert outcome.distinct_owners == 4
    assert outcome.owners_added == 1
    assert outcome.historical_inactive == 1

    def owner_of(year: int, nfl_team_id: int) -> Owner:
        team = session.execute(
            select(Team).where(
                Team.season_id == season_ids[year], Team.team_abbrev == str(nfl_team_id)
            )
        ).scalar_one()
        return session.get(Owner, team.owner_id)

    # 2024 already correct; 2023 required the A<->B swap + C->dave (a cycle the
    # permutation must resolve without violating UNIQUE(season_id, owner_id)).
    assert owner_of(2023, 1).display_name == "bob"
    assert owner_of(2023, 2).display_name == "alice"
    assert owner_of(2023, 3).display_name == "dave"
    assert owner_of(2024, 1).display_name == "alice"
    assert owner_of(2024, 2).display_name == "bob"
    assert owner_of(2024, 3).display_name == "carol"

    # dave is the inactive one-season manager; tenure recorded.
    dave = session.execute(select(Owner).where(Owner.nfl_user_id == "400")).scalar_one()
    assert dave.is_active is False
    assert (dave.joined_year, dave.left_year) == (2023, 2023)

    # Each season still has exactly 3 distinct owners (bijection preserved).
    for year in (2023, 2024):
        owners = (
            session.execute(select(Team.owner_id).where(Team.season_id == season_ids[year]))
            .scalars()
            .all()
        )
        assert len(set(owners)) == 3


class _AdamIllOwnersStub:
    _BY_YEAR: ClassVar[dict[int, dict[int, tuple[str, str]]]] = {
        2023: {1: ("800", "Adam"), 2: ("900", "Ill"), 3: ("100", "bob")},
        2024: {1: ("801", "adam"), 2: ("901", "ill"), 3: ("100", "bob")},
    }

    def get_html(self, url: str) -> str:
        year = 2023 if "/2023/" in url else 2024
        return _owners_html(self._BY_YEAR[year])


def _seed_adam_ill_seasons(session: Session) -> dict[int, int]:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    adam = Owner(league_id="36271", display_name="adam", nfl_user_id="800", is_active=True)
    ill = Owner(league_id="36271", display_name="ill", nfl_user_id="900", is_active=True)
    bob = Owner(league_id="36271", display_name="bob", nfl_user_id="100", is_active=True)
    session.add_all([adam, ill, bob])
    session.flush()
    season_ids: dict[int, int] = {}
    for year in (2023, 2024):
        season = Season(league_id="36271", year=year, status="completed")
        session.add(season)
        session.flush()
        season_ids[year] = season.season_id
        session.add_all(
            [
                Team(
                    season_id=season.season_id,
                    owner_id=adam.owner_id,
                    team_name="Team 1",
                    team_abbrev="1",
                ),
                Team(
                    season_id=season.season_id,
                    owner_id=ill.owner_id,
                    team_name="Team 2",
                    team_abbrev="2",
                ),
                Team(
                    season_id=season.season_id,
                    owner_id=bob.owner_id,
                    team_name="Team 3",
                    team_abbrev="3",
                ),
            ]
        )
    session.flush()
    return season_ids


def _seed_adam_ill_overrides(session: Session) -> None:
    for canonical, values in (
        (
            "adam",
            (
                ("display_name", "Adam"),
                ("display_name", "adam"),
                ("nfl_user_id", "800"),
                ("nfl_user_id", "801"),
            ),
        ),
        (
            "ill",
            (
                ("display_name", "Ill"),
                ("display_name", "ill"),
                ("nfl_user_id", "900"),
                ("nfl_user_id", "901"),
            ),
        ),
    ):
        for kind, value in values:
            seed_owner_identity_override(
                session,
                league_id="36271",
                external_id_kind=kind,
                external_id_value=value,
                canonical_display_name=canonical,
                notes=f"{canonical} same-name owner identity",
            )


@pytest.mark.integration
def test_reconstruct_owners_keeps_adam_and_ill_separate(session: Session) -> None:
    season_ids = _seed_adam_ill_seasons(session)
    _seed_adam_ill_overrides(session)
    adam = session.execute(select(Owner).where(Owner.display_name == "adam")).scalar_one()
    adam.aliases = {"display_names": ["Ill", "ill"], "nfl_user_ids": ["800", "900"]}

    outcome = reconstruct_owners(
        session,
        league_id="36271",
        fetcher=_AdamIllOwnersStub(),
        start_year=2023,
        end_year=2024,
    )
    session.commit()

    assert outcome.distinct_owners == 3

    adam = session.execute(select(Owner).where(Owner.display_name == "adam")).scalar_one()
    ill = session.execute(select(Owner).where(Owner.display_name == "ill")).scalar_one()
    assert adam.nfl_user_id is None
    assert ill.nfl_user_id is None
    assert adam.aliases == {"display_names": ["Adam"], "nfl_user_ids": ["800", "801"]}
    assert ill.aliases == {"display_names": ["Ill"], "nfl_user_ids": ["900", "901"]}
    for year in (2023, 2024):
        adam_team = session.execute(
            select(Team).where(Team.season_id == season_ids[year], Team.team_abbrev == "1")
        ).scalar_one()
        ill_team = session.execute(
            select(Team).where(Team.season_id == season_ids[year], Team.team_abbrev == "2")
        ).scalar_one()
        assert adam_team.owner_id == adam.owner_id
        assert ill_team.owner_id == ill.owner_id


@pytest.mark.integration
def test_current_owner_upsert_keeps_adam_and_ill_separate(session: Session) -> None:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    session.flush()
    _seed_adam_ill_overrides(session)

    owner_counts, team_counts = _upsert_owners_and_teams(
        session,
        league_id="36271",
        season_id=season.season_id,
        parsed=[
            ParsedOwner(
                team_id=1,
                team_name="Team Adam",
                display_name="Adam",
                nfl_user_id="800",
            ),
            ParsedOwner(
                team_id=2,
                team_name="Team Ill",
                display_name="Ill",
                nfl_user_id="900",
            ),
            ParsedOwner(
                team_id=3,
                team_name="Team Bob",
                display_name="bob",
                nfl_user_id="100",
            ),
        ],
    )
    session.commit()

    assert owner_counts.rows_added == 3
    assert team_counts.rows_added == 3
    adam = session.execute(select(Owner).where(Owner.display_name == "adam")).scalar_one()
    ill = session.execute(select(Owner).where(Owner.display_name == "ill")).scalar_one()
    assert adam.aliases == ["Adam"]
    assert ill.aliases == ["Ill"]


@pytest.mark.integration
def test_current_owner_upsert_allows_same_owner_multiple_teams_in_season(
    session: Session,
) -> None:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    session.flush()
    _seed_adam_ill_overrides(session)

    owner_counts, team_counts = _upsert_owners_and_teams(
        session,
        league_id="36271",
        season_id=season.season_id,
        parsed=[
            ParsedOwner(
                team_id=1,
                team_name="Team Adam",
                display_name="Adam",
                nfl_user_id="800",
            ),
            ParsedOwner(
                team_id=2,
                team_name="Team Adam 2",
                display_name="adam",
                nfl_user_id="801",
            ),
        ],
    )

    assert owner_counts.rows_added == 1
    assert team_counts.rows_added == 2
    adam = session.execute(select(Owner).where(Owner.display_name == "adam")).scalar_one()
    owner_ids = session.execute(select(Team.owner_id).where(Team.season_id == season.season_id))
    assert owner_ids.scalars().all() == [adam.owner_id, adam.owner_id]


@pytest.mark.integration
def test_current_owner_upsert_uses_nfl_team_id_across_renames(session: Session) -> None:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    owner = Owner(league_id="36271", display_name="dan", nfl_user_id="700", is_active=True)
    session.add(owner)
    season = Season(league_id="36271", year=2024, status="completed")
    session.add(season)
    session.flush()
    existing = Team(
        season_id=season.season_id,
        owner_id=owner.owner_id,
        team_name="Old Team Name",
        team_abbrev="7",
    )
    session.add(existing)
    session.flush()
    existing_id = existing.team_id

    owner_counts, team_counts = _upsert_owners_and_teams(
        session,
        league_id="36271",
        season_id=season.season_id,
        parsed=[
            ParsedOwner(
                team_id=7,
                team_name="Rev Russell's Sunday Service",
                display_name="dan",
                nfl_user_id="700",
            ),
        ],
    )
    session.commit()

    assert owner_counts.rows_added == 0
    assert team_counts.rows_added == 0
    assert team_counts.rows_updated == 1
    teams = session.execute(select(Team).where(Team.season_id == season.season_id)).scalars().all()
    assert len(teams) == 1
    assert teams[0].team_id == existing_id
    assert teams[0].team_name == "Rev Russell's Sunday Service"
