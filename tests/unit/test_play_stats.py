"""S6 play-by-play derivations, the Sleeper points-allowed convention, laterals."""

from __future__ import annotations

from typing import Any

from ff_pipeline.crawlers.nflverse.long_td_bonus import LongTdBonusKey, derive_long_td_bonus_counts
from ff_pipeline.crawlers.nflverse.play_stats import derive_play_counts
from ff_pipeline.crawlers.nflverse.team_defense import (
    _index_fantasy_points_allowed,
    build_team_defense_stats,
)

QB, WR, WR2, RB, ST = "00-QB", "00-WR", "00-WR2", "00-RB", "00-ST"


def _play(**kw: Any) -> dict[str, Any]:
    return {"season": 2026, "week": 3, **kw}


def _k(gsis: str) -> LongTdBonusKey:
    return LongTdBonusKey(gsis_id=gsis, season_year=2026, week=3)


def test_forty_plus_plays_per_role() -> None:
    rows = [
        _play(
            complete_pass=1,
            passing_yards=45,
            receiving_yards=45,
            passer_player_id=QB,
            receiver_player_id=WR,
        ),
        _play(
            complete_pass=1,
            passing_yards=39,
            receiving_yards=39,
            passer_player_id=QB,
            receiver_player_id=WR,
        ),
        # catch for 2, lateral for 80: the passer's 82 counts, the catcher's 2 doesn't
        _play(
            complete_pass=1,
            passing_yards=82,
            receiving_yards=2,
            passer_player_id=QB,
            receiver_player_id=WR2,
        ),
        _play(rush_attempt=1, rushing_yards=40, rusher_player_id=RB),
        # a 2-pt try never counts
        _play(rush_attempt=1, rushing_yards=40, rusher_player_id=RB, two_point_attempt=1),
    ]
    p = derive_play_counts(rows).player
    assert p[_k(QB)] == {"passing_completions_40_plus": 2.0}
    assert p[_k(WR)] == {"receptions_40_plus": 1.0}
    assert _k(WR2) not in p
    assert p[_k(RB)] == {"rushing_attempts_40_plus": 1.0}


def test_special_teams_fumble_recovery_is_the_kicking_teams() -> None:
    rows = [
        # kickoff: nflverse posteam = the RECEIVING team; KC kicks and recovers
        _play(
            play_type="kickoff",
            posteam="LV",
            defteam="KC",
            fumble_recovery_1_team="KC",
            fumbled_1_team="LV",
            fumble_recovery_1_player_id=ST,
        ),
        # punt: posteam = the punting team (SEA) recovers the muff
        _play(
            play_type="punt",
            posteam="SEA",
            defteam="SF",
            fumble_recovery_1_team="SEA",
            fumbled_1_team="SF",
            fumble_recovery_1_player_id=ST,
        ),
        # the returner recovers his own muff: not a recovery for anyone's DEF
        _play(
            play_type="punt",
            posteam="NYG",
            defteam="TEN",
            fumble_recovery_1_team="TEN",
            fumbled_1_team="TEN",
        ),
    ]
    out = derive_play_counts(rows)
    assert out.player[_k(ST)] == {"special_teams_fumble_recoveries": 2.0}
    assert out.team == {
        (2026, 3, "KC"): {"defense_special_teams_fumble_recoveries": 1.0},
        (2026, 3, "SEA"): {"defense_special_teams_fumble_recoveries": 1.0},
    }


def test_blocked_kicks_and_defensive_two_points() -> None:
    rows = [
        _play(play_type="punt", posteam="BUF", defteam="MIA", punt_blocked=1),
        _play(play_type="field_goal", posteam="BUF", defteam="MIA", field_goal_result="blocked"),
        _play(
            play_type="extra_point",
            posteam="CAR",
            defteam="TB",
            extra_point_result="blocked",
            defensive_extra_point_conv=1,
        ),
        _play(play_type="field_goal", posteam="BUF", defteam="MIA", field_goal_result="made"),
    ]
    team = derive_play_counts(rows).team
    assert team[(2026, 3, "MIA")] == {"blocked_kicks": 2.0}
    assert team[(2026, 3, "TB")] == {"blocked_kicks": 1.0, "defensive_2pt_returns": 1.0}


def _game() -> list[dict[str, Any]]:
    """TB 14 at CAR 0: an offensive TD + PAT, then a pick-six + PAT."""
    base = {"season": 2026, "week": 7, "game_id": "g", "home_team": "CAR", "away_team": "TB"}
    return [
        {**base, "total_home_score": 0, "total_away_score": 0},
        {
            **base,
            "total_home_score": 0,
            "total_away_score": 6,
            "touchdown": 1,
            "td_team": "TB",
            "posteam": "TB",
            "play_type": "pass",
        },
        {
            **base,
            "total_home_score": 0,
            "total_away_score": 7,
            "posteam": "TB",
            "extra_point_result": "good",
            "play_type": "extra_point",
        },
        {
            **base,
            "total_home_score": 0,
            "total_away_score": 13,
            "touchdown": 1,
            "td_team": "TB",
            "posteam": "CAR",
            "play_type": "pass",
        },
        {
            **base,
            "total_home_score": 0,
            "total_away_score": 14,
            "posteam": "TB",
            "extra_point_result": "good",
            "play_type": "extra_point",
        },
    ]


def test_points_allowed_convention_per_season() -> None:
    nfl_com = _index_fantasy_points_allowed(_game())
    sleeper = _index_fantasy_points_allowed(_game(), conversions_always_charged=frozenset({2026}))
    assert nfl_com[(2026, 7, "CAR")] == 7.0  # pick-six and its PAT both excluded
    assert sleeper[(2026, 7, "CAR")] == 8.0  # only the return TD's 6 excluded


def test_team_defense_rows_carry_zero_filled_play_counts() -> None:
    team_rows = [
        {"season": 2026, "week": 7, "team": t, "season_type": "REG"} for t in ("CAR", "TB")
    ]
    out = build_team_defense_stats(team_rows=team_rows, schedule_rows=[], play_by_play_rows=_game())
    by = {r.nfl_team: r.stats for r in out}
    assert by["CAR"]["blocked_kicks"] == 0.0
    assert by["TB"]["defensive_2pt_returns"] == 0.0
    no_pbp = build_team_defense_stats(team_rows=team_rows, schedule_rows=[])
    assert "blocked_kicks" not in no_pbp[0].stats


def test_long_td_bonus_goes_to_the_lateral_scorer() -> None:
    row = _play(
        pass_touchdown=1,
        passing_yards=82,
        receiving_yards=2,
        passer_player_id=QB,
        receiver_player_id=WR,
        lateral_receiver_player_id=WR2,
        lateral_receiving_yards=80,
        td_player_id=WR2,
    )
    out = derive_long_td_bonus_counts([row])
    assert out[_k(QB)] == {
        "passing_yards_bonus_long_td_40": 1.0,
        "passing_yards_bonus_long_td_50": 1.0,
    }
    assert out[_k(WR2)] == {
        "receiving_yards_bonus_long_td_40": 1.0,
        "receiving_yards_bonus_long_td_50": 1.0,
    }
    assert _k(WR) not in out
