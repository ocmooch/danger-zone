"""Unit tests for season-correct NFL-position helpers.

Covers the three pieces of the season-correct-position work:

* ``runner._upsert_season_positions`` derives one modal position per
  (player, season) from nflverse weekly stats (ties → latest week), folding
  HB/FB onto RB.
* ``queries.player_season_positions`` / ``player_position`` read those rows back
  and omit player-seasons with nothing stored (caller falls back to the snapshot).
* ``player_position_integrity.season_position_divergences`` reports players whose
  static ``players.position`` disagrees with a rostered season's position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from ff_pipeline.crawlers.nflverse.client import NflverseRosterPosition
from ff_pipeline.crawlers.nflverse.runner import _upsert_season_positions
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Owner,
    Player,
    PlayerSeasonPosition,
    Season,
    Team,
    TeamRoster,
)
from ff_pipeline.repository.player_position_integrity import season_position_divergences
from ff_pipeline.repository.queries import player_position, player_season_positions


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'season_pos.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _stat(gsis: str, season: int, week: int, position: str | None) -> NflverseRosterPosition:
    return NflverseRosterPosition(
        gsis_id=gsis,
        season_year=season,
        week=week,
        position=position,
    )


def test_upsert_season_positions_picks_modal_then_latest(session: Session) -> None:
    player = Player(name_full="Converted Back", position="RB", gsis_id="00-001")
    session.add(player)
    session.flush()
    gsis_map = {"00-001": player.player_id}

    # 2014: WR x2, RB x1 → modal WR. 2016: WR x1 (wk1), RB x1 (wk5) → tie, latest wins → RB.
    stats = [
        _stat("00-001", 2014, 1, "WR"),
        _stat("00-001", 2014, 2, "WR"),
        _stat("00-001", 2014, 3, "RB"),
        _stat("00-001", 2016, 1, "WR"),
        _stat("00-001", 2016, 5, "RB"),
    ]
    _upsert_season_positions(session, stats, gsis_map)
    session.commit()

    assert player_position(session, player.player_id, 2014) == "WR"
    assert player_position(session, player.player_id, 2016) == "RB"


def test_upsert_folds_hb_fb_onto_rb_and_skips_unresolved(session: Session) -> None:
    player = Player(name_full="Halfback", position="RB", gsis_id="00-002")
    session.add(player)
    session.flush()
    stats = [
        _stat("00-002", 2018, 1, "HB"),
        _stat("00-002", 2018, 2, "FB"),
        _stat("00-002", 2018, 3, None),  # null position ignored
        _stat("00-999", 2018, 1, "WR"),  # gsis not in map → skipped
    ]
    _upsert_season_positions(session, stats, {"00-002": player.player_id})
    session.commit()

    assert player_position(session, player.player_id, 2018) == "RB"
    # The unresolved gsis produced no row at all.
    assert session.query(PlayerSeasonPosition).count() == 1


def test_player_season_positions_batches_and_falls_back(session: Session) -> None:
    a = Player(name_full="A", position="TE", gsis_id="00-00a")
    b = Player(name_full="B", position="WR", gsis_id="00-00b")
    session.add_all([a, b])
    session.flush()
    # Only A has a stored season position; B falls back to its snapshot.
    session.add(
        PlayerSeasonPosition(
            player_id=a.player_id, season_year=2014, position="WR", source="nflverse"
        )
    )
    session.commit()

    resolved = player_season_positions(session, [a.player_id, b.player_id], 2014)
    assert resolved == {a.player_id: "WR"}
    assert player_season_positions(session, [], 2014) == {}


def _roster(session: Session, team: Team, player: Player, season_year: int) -> None:
    session.add(
        TeamRoster(
            team_id=team.team_id,
            player_id=player.player_id,
            season_year=season_year,
            week=14,
            roster_slot="WR",
            is_starter=True,
        )
    )


def test_season_position_divergences_reports_rostered_mismatch(session: Session) -> None:
    league = League(league_id="36271", name="DZ", platform="nfl_com")
    season = Season(league_id="36271", year=2014, status="completed")
    owner = Owner(league_id="36271", display_name="Harry")
    session.add_all([league, season, owner])
    session.flush()
    team = Team(season_id=season.season_id, owner_id=owner.owner_id, team_name="Harry")
    # Mislabeled: snapshot TE, but rostered + played WR in 2014/2015.
    jm = Player(name_full="Jordan Matthews", position="TE", gsis_id="00-0031299")
    # Clean: snapshot RB, season HB folds to RB → no divergence.
    fb = Player(name_full="Fullback", position="RB", gsis_id="00-00fb")
    session.add_all([team, jm, fb])
    session.flush()
    for yr in (2014, 2015):
        _roster(session, team, jm, yr)
        session.add(
            PlayerSeasonPosition(
                player_id=jm.player_id, season_year=yr, position="WR", source="nflverse"
            )
        )
    _roster(session, team, fb, 2014)
    session.add(
        PlayerSeasonPosition(
            player_id=fb.player_id, season_year=2014, position="HB", source="nflverse"
        )
    )
    session.commit()

    out = season_position_divergences(session)
    assert len(out) == 1
    entry = out[0]
    assert entry["name_full"] == "Jordan Matthews"
    assert entry["snapshot_position"] == "TE"
    assert entry["divergent_season_count"] == 2
    assert [s["season_year"] for s in entry["divergent_seasons"]] == [2014, 2015]
    assert {s["season_position"] for s in entry["divergent_seasons"]} == {"WR"}
