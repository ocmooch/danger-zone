"""Unit tests for the team-defense (DST) rollup and its scoring.

No network / DB: feeds hand-built team-stat + schedule rows through the
rollup and checks the projected stat dict, then scores it with a
faithful copy of The Danger Zone's ``defense`` rules and asserts the
engine total matches a hand computation. Exercises the bracket edges for
``points_allowed`` and ``total_yards_allowed`` (the all-or-nothing flat
rules that DST scoring hinges on).
"""

from __future__ import annotations

import pytest

from ff_pipeline.crawlers.nflverse.team_defense import build_team_defense_stats
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule, ScoringRules

# ---------------------------------------------------------------------------
# The Danger Zone defense rules (mirrors the seeded scoring_rules rows).
# ---------------------------------------------------------------------------

_POINTS_ALLOWED_BRACKETS = [
    (0.0, 0.0, 10.0),
    (1.0, 6.0, 7.0),
    (7.0, 13.0, 4.0),
    (14.0, 20.0, 1.0),
    (21.0, 27.0, 0.0),
    (28.0, 34.0, -1.0),
    (35.0, None, -4.0),
]
_YARDS_ALLOWED_BRACKETS = [
    (0.0, 99.0, 10.0),
    (100.0, 199.0, 7.0),
    (200.0, 299.0, 4.0),
    (300.0, 399.0, 1.0),
    (400.0, 449.0, 0.0),
    (450.0, 499.0, -1.0),
    (500.0, None, -4.0),
]


def _defense_rules() -> ScoringRules:
    rules: list[ScoringRule] = [
        ScoringRule(category="defense", stat_key="sacks", points_per_unit=1.0, unit_size=1.0),
        ScoringRule(
            category="defense", stat_key="interceptions", points_per_unit=2.0, unit_size=1.0
        ),
        ScoringRule(
            category="defense", stat_key="fumbles_recovered", points_per_unit=2.0, unit_size=1.0
        ),
        ScoringRule(category="defense", stat_key="safeties", points_per_unit=2.0, unit_size=1.0),
        ScoringRule(
            category="defense", stat_key="defensive_tds", points_per_unit=6.0, unit_size=1.0
        ),
        ScoringRule(
            category="defense", stat_key="special_teams_tds", points_per_unit=6.0, unit_size=1.0
        ),
    ]
    for lo, hi, pts in _POINTS_ALLOWED_BRACKETS:
        rules.append(
            ScoringRule(
                category="defense",
                stat_key="points_allowed",
                points_per_unit=0.0,
                flat_points=pts,
                threshold_min=lo,
                threshold_max=hi,
            )
        )
    for lo, hi, pts in _YARDS_ALLOWED_BRACKETS:
        rules.append(
            ScoringRule(
                category="defense",
                stat_key="total_yards_allowed",
                points_per_unit=0.0,
                flat_points=pts,
                threshold_min=lo,
                threshold_max=hi,
            )
        )
    return ScoringRules(season_id=1, rules=tuple(rules))


def _team_row(team: str, *, week: int = 3, **stats: float) -> dict[str, object]:
    base: dict[str, object] = {
        "season": 2024,
        "week": week,
        "team": team,
        "season_type": "REG",
        "passing_yards": 0,
        "rushing_yards": 0,
        "sack_yards_lost": 0,
        "sacks_suffered": 0,
        "def_sacks": 0,
        "def_interceptions": 0,
        "fumble_recovery_opp": 0,
        "def_safeties": 0,
        "def_tds": 0,
        "special_teams_tds": 0,
    }
    base.update(stats)
    return base


# ---------------------------------------------------------------------------
# Rollup shape
# ---------------------------------------------------------------------------


def test_rollup_builds_expected_stat_dict() -> None:
    team_rows = [
        # SF's own def_sacks (3) is the undercounted defense-side number; the
        # authoritative sack count is DAL's offense-side sacks_suffered (4).
        _team_row(
            "SF",
            passing_yards=300,
            rushing_yards=120,
            def_sacks=3,
            def_interceptions=2,
            fumble_recovery_opp=1,
            def_tds=1,
        ),
        _team_row("DAL", passing_yards=180, rushing_yards=70, sacks_suffered=4),
    ]
    schedule_rows = [
        {
            "season": 2024,
            "week": 3,
            "game_type": "REG",
            "home_team": "SF",
            "away_team": "DAL",
            "home_score": 27,
            "away_score": 0,
        }
    ]
    out = {
        t.nfl_team: t
        for t in build_team_defense_stats(team_rows=team_rows, schedule_rows=schedule_rows)
    }

    sf = out["SF"]
    assert sf.nfl_opponent == "DAL"
    # Sourced from DAL's sacks_suffered (4), not SF's def_sacks (3).
    assert sf.stats["sacks"] == 4.0
    assert sf.stats["interceptions"] == 2.0
    assert sf.stats["fumbles_recovered"] == 1.0
    assert sf.stats["defensive_tds"] == 1.0
    # SF shut DAL out and allowed DAL's 250 offensive yards.
    assert sf.stats["points_allowed"] == 0.0
    assert sf.stats["total_yards_allowed"] == 250.0


def test_rollup_scores_to_hand_computed_total() -> None:
    team_rows = [
        _team_row(
            "SF",
            passing_yards=300,
            rushing_yards=120,
            def_interceptions=2,
            fumble_recovery_opp=1,
            def_tds=1,
        ),
        # SF's 4 sacks come from DAL's offense-side sacks_suffered.
        _team_row("DAL", passing_yards=180, rushing_yards=70, sacks_suffered=4),
    ]
    schedule_rows = [
        {
            "season": 2024,
            "week": 3,
            "game_type": "REG",
            "home_team": "SF",
            "away_team": "DAL",
            "home_score": 27,
            "away_score": 0,
        }
    ]
    sf = next(
        t
        for t in build_team_defense_stats(team_rows=team_rows, schedule_rows=schedule_rows)
        if t.nfl_team == "SF"
    )
    result = apply_rules(sf.stats, _defense_rules())
    # 4 sacks(4) + 2 INT(4) + 1 fum(2) + 1 def TD(6) + shutout(10) + 250 yds bracket(4) = 30
    assert result.total_points == 30.0


def test_total_yards_allowed_is_net_of_sacks() -> None:
    """The opponent's yards allowed are *net*: sack yardage is subtracted
    from passing, matching the official NFL total.

    nflreadpy stores ``sack_yards_lost`` **negative**, so this fixture uses
    -30 — the regression guard for the sign bug where ``gained - signed``
    double-negated and *added* sack yards (250+70-(-30)=350) instead of
    removing them (250+70-30=290).
    """
    team_rows = [
        _team_row("SF", passing_yards=0, rushing_yards=0),
        # DAL gained 320 gross but lost 30 to sacks -> 290 net.
        _team_row("DAL", passing_yards=250, rushing_yards=70, sack_yards_lost=-30),
    ]
    schedule_rows = [
        {
            "season": 2024,
            "week": 3,
            "game_type": "REG",
            "home_team": "SF",
            "away_team": "DAL",
            "home_score": 17,
            "away_score": 10,
        }
    ]
    sf = next(
        t
        for t in build_team_defense_stats(team_rows=team_rows, schedule_rows=schedule_rows)
        if t.nfl_team == "SF"
    )
    assert sf.stats["total_yards_allowed"] == 290.0


def test_real_game_dal_dst_week11_2023_scores_28() -> None:
    """Known-answer regression from real nflverse data: DAL DEF vs CAR,
    2023 week 11, must score 28 (matches NFL.com).

    The stat values below are exactly what nflreadpy 0.1.5 returns. This
    case is what surfaced the two bugs:

    * yards: CAR offense passing=123, rushing=110, sack_yards_lost=-46 ->
      net 187 (the 100-199 bracket = 7 pts), not the buggy 279 (4 pts);
    * sacks: DAL def_sacks=6 undercounts; CAR's sacks_suffered=7 is the
      authoritative count (7 pts, not 6).

    7 sacks(7) + 1 INT(2) + 1 fum(2) + 1 def TD(6) + 10 pts allowed(4)
    + 187 yds(7) = 28.
    """
    team_rows = [
        _team_row(
            "DAL",
            week=11,
            season=2023,
            passing_yards=204,
            rushing_yards=107,
            def_sacks=6,  # undercount — must be overridden by CAR.sacks_suffered
            def_interceptions=1,
            fumble_recovery_opp=1,
            def_tds=1,
        ),
        _team_row(
            "CAR",
            week=11,
            season=2023,
            passing_yards=123,
            rushing_yards=110,
            sack_yards_lost=-46,  # nflreadpy stores this negative
            sacks_suffered=7,
        ),
    ]
    schedule_rows = [
        {
            "season": 2023,
            "week": 11,
            "game_type": "REG",
            "home_team": "DAL",
            "away_team": "CAR",
            "home_score": 33,
            "away_score": 10,
        }
    ]
    dal = next(
        t
        for t in build_team_defense_stats(team_rows=team_rows, schedule_rows=schedule_rows)
        if t.nfl_team == "DAL"
    )
    assert dal.stats["sacks"] == 7.0
    assert dal.stats["total_yards_allowed"] == 187.0
    assert dal.stats["points_allowed"] == 10.0
    assert apply_rules(dal.stats, _defense_rules()).total_points == 28.0


def test_missing_schedule_omits_bracket_keys() -> None:
    """No schedule row → no points_allowed / total_yards_allowed key, so the
    flat brackets score nothing rather than awarding a spurious shutout/
    sub-100-yard bonus."""
    team_rows = [_team_row("SF", def_sacks=3)]
    out = build_team_defense_stats(team_rows=team_rows, schedule_rows=[])
    sf = out[0]
    assert "points_allowed" not in sf.stats
    assert "total_yards_allowed" not in sf.stats
    # Only the 3 sacks score; no bonus leaks in from a defaulted-zero bracket.
    assert apply_rules(sf.stats, _defense_rules()).total_points == 3.0


@pytest.mark.parametrize(
    ("points_allowed", "expected"),
    [(0, 10.0), (6, 7.0), (7, 4.0), (20, 1.0), (21, 0.0), (28, -1.0), (35, -4.0), (52, -4.0)],
)
def test_points_allowed_brackets(points_allowed: int, expected: float) -> None:
    stats = {"points_allowed": float(points_allowed)}
    assert apply_rules(stats, _defense_rules()).total_points == expected


@pytest.mark.parametrize(
    ("yards", "expected"),
    [(99, 10.0), (100, 7.0), (199, 7.0), (299, 4.0), (449, 0.0), (499, -1.0), (500, -4.0)],
)
def test_total_yards_allowed_brackets(yards: int, expected: float) -> None:
    stats = {"total_yards_allowed": float(yards)}
    assert apply_rules(stats, _defense_rules()).total_points == expected
