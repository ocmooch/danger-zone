"""Sleeper scoring_settings → rules, and the S2 identity through our engine (S6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRule, ScoringRules
from ff_pipeline.scoring.scraper import parse_settings_csv
from ff_pipeline.scoring.sleeper_rules import (
    RuleRow,
    UnmappedScoringKeyError,
    derive_rules,
    diff_rules,
    sleeper_stats_to_engine,
)

FX = Path(__file__).resolve().parents[1] / "fixtures"
SEASON = FX / "sleeper_league" / "season_2026"
LEAGUE_FILE = SEASON / "v1_league_900000000000000001.json"

NEW_IN_2026 = {
    ("defense", "blocked_kicks", 2.0),
    ("defense", "defensive_2pt_returns", 2.0),
    ("misc", "fumble_return_tds", 6.0),
    ("misc", "special_teams_fumble_recoveries", 2.0),
    ("passing", "passing_completions_40_plus", 1.0),
    ("receiving", "receptions_40_plus", 1.0),
    ("rushing", "rushing_attempts_40_plus", 1.0),
}


def _settings() -> dict[str, float]:
    return dict(json.loads(LEAGUE_FILE.read_text())["scoring_settings"])


def _rules(rows: list[RuleRow]) -> ScoringRules:
    return ScoringRules(
        season_id=0,
        rules=tuple(
            ScoringRule(
                r.category,
                r.stat_key,
                r.points_per_unit,
                r.unit_size,
                r.threshold_min,
                r.threshold_max,
                r.flat_points,
            )
            for r in rows
        ),
    )


def _rows_2025() -> list[RuleRow]:
    parsed = parse_settings_csv(FX / "scoring_rules" / "36271_2025.csv")
    return [
        RuleRow(
            p.rule.category,
            p.rule.stat_key,
            p.rule.points_per_unit,
            p.rule.unit_size,
            p.rule.threshold_min,
            p.rule.threshold_max,
            p.rule.flat_points,
        )
        for p in parsed.rules
    ]


def test_d10_every_2025_rule_is_identical_plus_the_new_rows() -> None:
    d = diff_rules(_rows_2025(), derive_rules(_settings()))
    assert d.only_in_a == ()
    assert d.same == 51
    assert {(r.category, r.stat_key, r.points_per_unit) for r in d.only_in_b} == NEW_IN_2026


def test_rates_tiers_and_merged_brackets() -> None:
    rows = derive_rules(_settings())
    by = {(r.stat_key, r.threshold_min): r for r in rows}
    assert (by[("passing_yards", 0.0)].points_per_unit, by[("passing_yards", 0.0)].unit_size) == (
        1.0,
        25.0,
    )
    assert (
        by[("passing_yards", 300.0)].threshold_max,
        by[("passing_yards", 300.0)].flat_points,
    ) == (
        399.0,
        1.0,
    )
    assert by[("total_yards_allowed", 300.0)].threshold_max == 399.0  # 300-349 + 350-399
    assert by[("total_yards_allowed", 500.0)].threshold_max is None  # 500-549 + 550+
    assert by[("field_goal_made_50_plus", 0.0)].points_per_unit == 5.0  # fgm 3 + fgm_50_59 2
    assert by[("points_allowed", 21.0)].flat_points == 0.0  # zero bracket kept, as in 2025
    # def_st_fum_rec == fum_rec: nflverse's fumbles_recovered already counts them
    assert not any(r.stat_key == "defense_special_teams_fumble_recoveries" for r in rows)


def test_zero_keys_skipped_unknown_nonzero_aborts() -> None:
    s = _settings()
    s["bonus_fd_wr"] = 0.0
    assert derive_rules(s) == derive_rules(_settings())
    s["bonus_fd_wr"] = 0.5
    s["idp_tkl"] = 1.0
    with pytest.raises(UnmappedScoringKeyError) as exc:
        derive_rules(s)
    assert exc.value.keys == ("bonus_fd_wr", "idp_tkl")


def test_unequal_long_fg_tiers_abort_and_st_delta_is_written() -> None:
    s = _settings()
    s["fgm_60p"] = 3.0
    with pytest.raises(UnmappedScoringKeyError):
        derive_rules(s)
    s = _settings()
    s["def_st_fum_rec"] = 3.0
    rows = derive_rules(s)
    st = [r for r in rows if r.stat_key == "defense_special_teams_fumble_recoveries"]
    assert [r.points_per_unit for r in st] == [1.0]


def test_engine_reproduces_sleeper_points_from_sleeper_stats() -> None:
    """The S2 identity (points = stats x settings) holds through our engine."""
    rules = _rules(derive_rules(_settings()))
    n = 0
    for week in range(1, 10):
        stats = {
            r["player_id"]: r["stats"]
            for r in json.loads(
                (SEASON / f"stats_nfl_2026_{week}_season_type_regular.json").read_text()
            )
        }
        matchups = json.loads(
            (SEASON / f"v1_league_900000000000000001_matchups_{week}.json").read_text()
        )
        for m in matchups:
            for sid, official in (m.get("players_points") or {}).items():
                mapped = sleeper_stats_to_engine(
                    stats.get(sid, {}), is_team_defense=not sid.isdigit()
                )
                assert apply_rules(mapped, rules).total_points == pytest.approx(
                    official, abs=0.01
                ), (week, sid)
                n += 1
    assert n > 1500


def test_shutout_defense_without_pts_allow_scores_the_zero_bracket() -> None:
    rules = _rules(derive_rules(_settings()))
    mapped = sleeper_stats_to_engine(
        {"gp": 1.0, "int": 1.0, "yds_allow": 250.0}, is_team_defense=True
    )
    assert mapped["points_allowed"] == 0.0
    assert apply_rules(mapped, rules).total_points == 2 + 10 + 4
