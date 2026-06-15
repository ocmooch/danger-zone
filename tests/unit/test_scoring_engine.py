"""Unit tests for ``ff_pipeline.scoring.engine``.

These tests cover every stat key the engine handles, each bonus rule
at its on/off boundary, multi-stat composition, negative stats, and
the missing-key / unmapped-stat behaviours documented in
``docs/05_SCORING_ENGINE.md``. They're the correctness oracle that
every other milestone depends on — keep them pedantic.
"""

from __future__ import annotations

import pytest

from ff_pipeline.scoring import ScoredResult, ScoringRule, ScoringRules, apply_rules

# ---------------------------------------------------------------------------
# Fixtures: a representative standard-PPR rules set covering every stat key
# in docs/05_SCORING_ENGINE.md. Numbers are typical NFL.com defaults; tests
# rely on the ratios, not on any particular league's exact values.
# ---------------------------------------------------------------------------


def _rule(
    category: str,
    stat_key: str,
    *,
    points_per_unit: float = 0.0,
    unit_size: float = 1.0,
    threshold_min: float | None = None,
    threshold_max: float | None = None,
    flat_points: float | None = None,
) -> ScoringRule:
    return ScoringRule(
        category=category,
        stat_key=stat_key,
        points_per_unit=points_per_unit,
        unit_size=unit_size,
        threshold_min=threshold_min,
        threshold_max=threshold_max,
        flat_points=flat_points,
    )


STD_PPR_RULES = ScoringRules(
    season_id=1,
    rules=(
        # Passing
        _rule("passing", "passing_yards", points_per_unit=1.0, unit_size=25.0),
        _rule("passing", "passing_tds", points_per_unit=4.0),
        _rule("passing", "passing_interceptions", points_per_unit=-2.0),
        _rule("passing", "passing_2pt_conversions", points_per_unit=2.0),
        _rule("passing", "passing_yards", flat_points=3.0, threshold_min=300.0),
        _rule("passing", "passing_yards", flat_points=5.0, threshold_min=400.0),
        _rule("passing", "passing_yards_bonus_long_td_40", points_per_unit=2.0),
        # Rushing
        _rule("rushing", "rushing_yards", points_per_unit=1.0, unit_size=10.0),
        _rule("rushing", "rushing_tds", points_per_unit=6.0),
        _rule("rushing", "rushing_2pt_conversions", points_per_unit=2.0),
        _rule("rushing", "rushing_yards", flat_points=3.0, threshold_min=100.0),
        _rule("rushing", "rushing_yards", flat_points=5.0, threshold_min=200.0),
        _rule("rushing", "rushing_yards_bonus_long_td_40", points_per_unit=2.0),
        # Receiving
        _rule("receiving", "receptions", points_per_unit=1.0),
        _rule("receiving", "receiving_yards", points_per_unit=1.0, unit_size=10.0),
        _rule("receiving", "receiving_tds", points_per_unit=6.0),
        _rule("receiving", "receiving_2pt_conversions", points_per_unit=2.0),
        _rule("receiving", "receiving_yards", flat_points=3.0, threshold_min=100.0),
        _rule("receiving", "receiving_yards", flat_points=5.0, threshold_min=200.0),
        _rule("receiving", "receiving_yards_bonus_long_td_40", points_per_unit=2.0),
        # Misc offensive
        _rule("misc", "fumbles_lost", points_per_unit=-2.0),
        _rule("misc", "fumble_return_tds", points_per_unit=6.0),
        # Kicking
        _rule("kicking", "field_goal_made_0_19", points_per_unit=3.0),
        _rule("kicking", "field_goal_made_20_29", points_per_unit=3.0),
        _rule("kicking", "field_goal_made_30_39", points_per_unit=3.0),
        _rule("kicking", "field_goal_made_40_49", points_per_unit=4.0),
        _rule("kicking", "field_goal_made_50_plus", points_per_unit=5.0),
        _rule("kicking", "extra_point_made", points_per_unit=1.0),
        _rule("kicking", "field_goal_missed", points_per_unit=-1.0),
        _rule("kicking", "extra_point_missed", points_per_unit=-1.0),
        # Defense / ST
        _rule("defense", "sacks", points_per_unit=1.0),
        _rule("defense", "interceptions", points_per_unit=2.0),
        _rule("defense", "fumbles_recovered", points_per_unit=2.0),
        _rule("defense", "safeties", points_per_unit=2.0),
        _rule("defense", "defensive_tds", points_per_unit=6.0),
        _rule("defense", "special_teams_tds", points_per_unit=6.0),
        _rule("defense", "blocked_kicks", points_per_unit=2.0),
        _rule(
            "defense",
            "points_allowed",
            flat_points=10.0,
            threshold_min=0.0,
            threshold_max=0.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=7.0,
            threshold_min=1.0,
            threshold_max=6.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=4.0,
            threshold_min=7.0,
            threshold_max=13.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=1.0,
            threshold_min=14.0,
            threshold_max=20.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=0.0,
            threshold_min=21.0,
            threshold_max=27.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=-1.0,
            threshold_min=28.0,
            threshold_max=34.0,
        ),
        _rule(
            "defense",
            "points_allowed",
            flat_points=-4.0,
            threshold_min=35.0,
        ),
    ),
)


# ---------------------------------------------------------------------------
# Per-stat-key tests: each key independently produces the expected points.
# ---------------------------------------------------------------------------


def test_passing_yards_per_25() -> None:
    result = apply_rules({"passing_yards": 250}, STD_PPR_RULES)
    assert result.total_points == 10.0
    assert result.breakdown == {"passing": 10.0}


def test_passing_yards_fractional() -> None:
    result = apply_rules({"passing_yards": 312}, STD_PPR_RULES)
    # 312/25 = 12.48 + 300+ bonus 3 = 15.48
    assert result.total_points == 15.48


def test_passing_tds() -> None:
    result = apply_rules({"passing_tds": 3}, STD_PPR_RULES)
    assert result.total_points == 12.0


def test_passing_interceptions_negative() -> None:
    result = apply_rules({"passing_interceptions": 2}, STD_PPR_RULES)
    assert result.total_points == -4.0
    assert result.breakdown == {"passing": -4.0}


def test_passing_2pt_conversion() -> None:
    result = apply_rules({"passing_2pt_conversions": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_passing_long_td_bonus_per_unit() -> None:
    # Two long TDs * 2 pts each
    result = apply_rules({"passing_yards_bonus_long_td_40": 2}, STD_PPR_RULES)
    assert result.total_points == 4.0


def test_rushing_yards_per_10() -> None:
    result = apply_rules({"rushing_yards": 80}, STD_PPR_RULES)
    assert result.total_points == 8.0


def test_rushing_tds() -> None:
    result = apply_rules({"rushing_tds": 2}, STD_PPR_RULES)
    assert result.total_points == 12.0


def test_rushing_2pt() -> None:
    result = apply_rules({"rushing_2pt_conversions": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_rushing_long_td_bonus_per_unit() -> None:
    result = apply_rules({"rushing_yards_bonus_long_td_40": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_receptions_ppr() -> None:
    result = apply_rules({"receptions": 7}, STD_PPR_RULES)
    assert result.total_points == 7.0
    assert result.breakdown == {"receiving": 7.0}


def test_receiving_yards_per_10() -> None:
    result = apply_rules({"receiving_yards": 95}, STD_PPR_RULES)
    assert result.total_points == 9.5


def test_receiving_tds() -> None:
    result = apply_rules({"receiving_tds": 2}, STD_PPR_RULES)
    assert result.total_points == 12.0


def test_receiving_2pt() -> None:
    result = apply_rules({"receiving_2pt_conversions": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_receiving_long_td_bonus_per_unit() -> None:
    result = apply_rules({"receiving_yards_bonus_long_td_40": 3}, STD_PPR_RULES)
    assert result.total_points == 6.0


def test_fumbles_lost_negative() -> None:
    result = apply_rules({"fumbles_lost": 1}, STD_PPR_RULES)
    assert result.total_points == -2.0
    assert result.breakdown == {"misc": -2.0}


def test_fumble_return_td() -> None:
    result = apply_rules({"fumble_return_tds": 1}, STD_PPR_RULES)
    assert result.total_points == 6.0


def test_field_goal_brackets() -> None:
    stats = {
        "field_goal_made_0_19": 1,
        "field_goal_made_20_29": 1,
        "field_goal_made_30_39": 1,
        "field_goal_made_40_49": 1,
        "field_goal_made_50_plus": 1,
    }
    result = apply_rules(stats, STD_PPR_RULES)
    # 3 + 3 + 3 + 4 + 5
    assert result.total_points == 18.0
    assert result.breakdown == {"kicking": 18.0}


def test_extra_point_made() -> None:
    result = apply_rules({"extra_point_made": 3}, STD_PPR_RULES)
    assert result.total_points == 3.0


def test_field_goal_missed_negative() -> None:
    result = apply_rules({"field_goal_missed": 2}, STD_PPR_RULES)
    assert result.total_points == -2.0


def test_extra_point_missed_negative() -> None:
    result = apply_rules({"extra_point_missed": 1}, STD_PPR_RULES)
    assert result.total_points == -1.0


def test_sacks() -> None:
    result = apply_rules({"sacks": 4}, STD_PPR_RULES)
    assert result.total_points == 4.0


def test_defense_interceptions() -> None:
    result = apply_rules({"interceptions": 2}, STD_PPR_RULES)
    assert result.total_points == 4.0


def test_defense_fumbles_recovered() -> None:
    result = apply_rules({"fumbles_recovered": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_safety() -> None:
    result = apply_rules({"safeties": 1}, STD_PPR_RULES)
    assert result.total_points == 2.0


def test_defensive_td() -> None:
    result = apply_rules({"defensive_tds": 1}, STD_PPR_RULES)
    assert result.total_points == 6.0


def test_special_teams_td() -> None:
    result = apply_rules({"special_teams_tds": 1}, STD_PPR_RULES)
    assert result.total_points == 6.0


def test_team_defense_special_teams_td_does_not_score_misc_duplicate() -> None:
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule("misc", "special_teams_tds", points_per_unit=6.0),
            _rule("defense", "special_teams_tds", points_per_unit=6.0),
            _rule("defense", "sacks", points_per_unit=1.0),
        ),
    )
    result = apply_rules({"special_teams_tds": 1, "sacks": 1}, rules)

    assert result.total_points == 7.0
    assert result.breakdown == {"defense": 7.0}


def test_individual_special_teams_td_still_scores_misc_rule() -> None:
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule("misc", "special_teams_tds", points_per_unit=6.0),
            _rule("defense", "special_teams_tds", points_per_unit=6.0),
        ),
    )
    result = apply_rules({"special_teams_tds": 1}, rules)

    assert result.total_points == 6.0
    assert result.breakdown == {"misc": 6.0}


def test_blocked_kicks() -> None:
    result = apply_rules({"blocked_kicks": 2}, STD_PPR_RULES)
    assert result.total_points == 4.0


# ---------------------------------------------------------------------------
# Bonus threshold on/off boundary tests.
# ---------------------------------------------------------------------------


def test_passing_300_bonus_just_below_threshold() -> None:
    result = apply_rules({"passing_yards": 299}, STD_PPR_RULES)
    # 299/25 = 11.96, no bonus
    assert result.total_points == 11.96


def test_passing_300_bonus_at_threshold() -> None:
    result = apply_rules({"passing_yards": 300}, STD_PPR_RULES)
    # 300/25 = 12.0 + 3.0 bonus
    assert result.total_points == 15.0


def test_passing_400_bonus_stacks_with_300() -> None:
    # 400+ should trigger BOTH the 300+ and 400+ bonuses
    result = apply_rules({"passing_yards": 400}, STD_PPR_RULES)
    # 400/25 = 16 + 3 + 5 = 24
    assert result.total_points == 24.0


def test_rushing_100_bonus_at_threshold() -> None:
    result = apply_rules({"rushing_yards": 100}, STD_PPR_RULES)
    # 100/10 = 10 + 3 = 13
    assert result.total_points == 13.0


def test_rushing_200_bonus_stacks_with_100() -> None:
    result = apply_rules({"rushing_yards": 215}, STD_PPR_RULES)
    # 215/10 = 21.5 + 3 + 5
    assert result.total_points == 29.5


def test_receiving_100_bonus_just_below() -> None:
    result = apply_rules({"receiving_yards": 99}, STD_PPR_RULES)
    assert result.total_points == 9.9


def test_receiving_200_bonus_at_threshold() -> None:
    result = apply_rules({"receiving_yards": 200}, STD_PPR_RULES)
    # 200/10 = 20 + 3 (100+) + 5 (200+)
    assert result.total_points == 28.0


# ---------------------------------------------------------------------------
# Points-allowed bracket tests (range-gated flat bonuses).
# ---------------------------------------------------------------------------


def test_points_allowed_shutout() -> None:
    result = apply_rules({"points_allowed": 0}, STD_PPR_RULES)
    assert result.total_points == 10.0


def test_points_allowed_low_bracket() -> None:
    result = apply_rules({"points_allowed": 6}, STD_PPR_RULES)
    assert result.total_points == 7.0


def test_points_allowed_top_of_bracket() -> None:
    # 13 falls in the 7-13 bracket
    result = apply_rules({"points_allowed": 13}, STD_PPR_RULES)
    assert result.total_points == 4.0


def test_points_allowed_mid_range_zero() -> None:
    result = apply_rules({"points_allowed": 24}, STD_PPR_RULES)
    # 21-27 bracket awards 0
    assert result.total_points == 0.0


def test_points_allowed_high_negative() -> None:
    result = apply_rules({"points_allowed": 35}, STD_PPR_RULES)
    assert result.total_points == -4.0


# ---------------------------------------------------------------------------
# Composition: multi-key stat lines.
# ---------------------------------------------------------------------------


def test_qb_stat_line_composition() -> None:
    # 312 pass yds, 2 pass TDs, 1 INT, 18 rush yds — the example from the doc + extras
    stats = {
        "passing_yards": 312,
        "passing_tds": 2,
        "passing_interceptions": 1,
        "rushing_yards": 18,
    }
    result = apply_rules(stats, STD_PPR_RULES)
    # passing: 312/25 + 2*4 + 1*-2 + 300+ bonus = 12.48 + 8 - 2 + 3 = 21.48
    # rushing: 18/10 = 1.8
    assert result.total_points == 23.28
    assert result.breakdown["passing"] == 21.48
    assert result.breakdown["rushing"] == 1.8


def test_rb_stat_line_composition() -> None:
    # 22 carries / 105 yards / 1 TD / 4 catches / 32 receiving yards
    stats = {
        "rushing_yards": 105,
        "rushing_tds": 1,
        "receptions": 4,
        "receiving_yards": 32,
    }
    result = apply_rules(stats, STD_PPR_RULES)
    # rushing: 10.5 + 6 + 3 (100+ bonus) = 19.5
    # receiving: 4 + 3.2 = 7.2
    assert result.total_points == 26.7
    assert result.breakdown == {"rushing": 19.5, "receiving": 7.2}


def test_kicker_stat_line_composition() -> None:
    stats = {
        "field_goal_made_30_39": 2,
        "field_goal_made_50_plus": 1,
        "extra_point_made": 3,
        "extra_point_missed": 1,
    }
    result = apply_rules(stats, STD_PPR_RULES)
    # 6 + 5 + 3 - 1
    assert result.total_points == 13.0


def test_defense_stat_line_composition() -> None:
    stats = {
        "sacks": 3,
        "interceptions": 1,
        "defensive_tds": 1,
        "points_allowed": 10,
    }
    result = apply_rules(stats, STD_PPR_RULES)
    # 3 + 2 + 6 + 4 (7-13 bracket)
    assert result.total_points == 15.0


# ---------------------------------------------------------------------------
# Missing / empty / unmapped stat behaviour.
# ---------------------------------------------------------------------------


def test_empty_stats_returns_zero() -> None:
    result = apply_rules({}, STD_PPR_RULES)
    assert result.total_points == 0.0
    assert result.breakdown == {}


def test_dnp_player_all_zeros_returns_zero() -> None:
    stats = {"passing_yards": 0, "passing_tds": 0, "rushing_yards": 0}
    result = apply_rules(stats, STD_PPR_RULES)
    assert result.total_points == 0.0


def test_missing_keys_default_to_zero() -> None:
    # Only passing_tds present; engine shouldn't error on absent keys
    result = apply_rules({"passing_tds": 1}, STD_PPR_RULES)
    assert result.total_points == 4.0


def test_unmapped_stat_is_reported() -> None:
    stats = {"passing_tds": 1, "mystery_stat": 42}
    result = apply_rules(stats, STD_PPR_RULES)
    # Unmapped stats surface in the result so callers can alert on
    # data-quality issues without parsing log output.
    assert result.unmapped_stats == ("mystery_stat",)
    assert result.total_points == 4.0  # known stat still scored


def test_multiple_unmapped_stats_sorted() -> None:
    stats = {"zebra_stat": 1, "alpha_stat": 1}
    result = apply_rules(stats, ScoringRules(season_id=1, rules=()))
    assert result.unmapped_stats == ("alpha_stat", "zebra_stat")


def test_zero_value_unmapped_stat_is_suppressed() -> None:
    # A stat present in raw data but zero can never affect scoring; suppress
    # it so leagues that don't score e.g. field_goal_missed don't flood logs.
    stats = {"passing_tds": 1, "field_goal_missed": 0.0}
    result = apply_rules(stats, STD_PPR_RULES)
    assert result.unmapped_stats == ()
    assert result.total_points == 4.0


def test_nonzero_unmapped_stat_is_still_reported() -> None:
    stats = {"passing_tds": 1, "mystery_stat": 7.0}
    result = apply_rules(stats, STD_PPR_RULES)
    assert result.unmapped_stats == ("mystery_stat",)


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_negative_per_unit_with_negative_stat_yields_positive() -> None:
    # Pathological but documented: rule with neg points_per_unit + neg stat
    rules = ScoringRules(
        season_id=1,
        rules=(_rule("misc", "weird", points_per_unit=-1.0),),
    )
    result = apply_rules({"weird": -3}, rules)
    assert result.total_points == 3.0


def test_negative_rushing_yards_accrue_negative_points() -> None:
    # A real game stat: 1 carry for -3 yards. NFL.com awards -0.3 pts under
    # the standard "1 pt per 10 yds" rushing rule. The engine must NOT clip
    # this to zero — the bug it backstops is that M4's loader emits
    # per-unit rules with threshold_min=0 (for SQL upsert idempotency), and
    # the engine used to clip *any* threshold_min, silently zeroing
    # negative yardage games.
    result = apply_rules({"rushing_yards": -3}, STD_PPR_RULES)
    assert result.total_points == pytest.approx(-0.3)
    assert result.breakdown == {"rushing": pytest.approx(-0.3)}


def test_negative_receiving_yards_accrue_negative_points() -> None:
    # Screen pass for a loss: 1 catch for -4 yards. Receptions still earn
    # the PPR point; yardage still subtracts.
    result = apply_rules({"receptions": 1, "receiving_yards": -4}, STD_PPR_RULES)
    # 1 PPR point + (-4 / 10 = -0.4) = 0.6
    assert result.total_points == pytest.approx(0.6)


def test_negative_passing_yards_accrue_negative_points_no_bonus() -> None:
    # Rare but real: a QB completes a screen behind the LOS for -5 yards
    # and is then pulled (injury / blowout) so end-of-game passing yards
    # are negative. NFL.com awards -0.2 under "1 pt / 25 yds" — and the
    # 300+/400+ flat bonuses must NOT fire on negative values (otherwise
    # every player without a passing line would get them).
    result = apply_rules({"passing_yards": -5}, STD_PPR_RULES)
    assert result.total_points == pytest.approx(-0.2)
    assert result.breakdown == {"passing": pytest.approx(-0.2)}


def test_negative_yards_with_threshold_min_zero_match_loader_shape() -> None:
    # Mirrors exactly what the M4 scoring scraper emits: per-unit rules
    # carry threshold_min=0.0 (not None) so the (season, cat, key,
    # threshold_min) unique constraint round-trips through SQL upsert.
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule(
                "rushing",
                "rushing_yards",
                points_per_unit=1.0,
                unit_size=10.0,
                threshold_min=0.0,
            ),
        ),
    )
    assert apply_rules({"rushing_yards": -3}, rules).total_points == pytest.approx(-0.3)
    assert apply_rules({"rushing_yards": 50}, rules).total_points == pytest.approx(5.0)


def test_threshold_min_positive_still_clips_negatives() -> None:
    # The "only yards above 100 count" semantic is preserved: a sub-100
    # game (including negative yardage games) earns zero from this rule.
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule(
                "rushing",
                "rushing_yards",
                points_per_unit=1.0,
                threshold_min=100.0,
            ),
        ),
    )
    assert apply_rules({"rushing_yards": -3}, rules).total_points == 0.0
    assert apply_rules({"rushing_yards": 99}, rules).total_points == 0.0
    assert apply_rules({"rushing_yards": 120}, rules).total_points == 20.0


def test_zero_unit_size_rule_skipped_safely() -> None:
    # A misconfigured rule (unit_size=0) would divide by zero — the
    # engine must skip it rather than blowing up the whole stat line.
    rules = ScoringRules(
        season_id=1,
        rules=(_rule("misc", "broken", points_per_unit=1.0, unit_size=0.0),),
    )
    result = apply_rules({"broken": 100}, rules)
    assert result.total_points == 0.0


def test_threshold_min_only_shifts_per_unit_window() -> None:
    # 1 point per 1 yard above 100, with no cap
    rules = ScoringRules(
        season_id=1,
        rules=(_rule("rushing", "rushing_yards", points_per_unit=1.0, threshold_min=100.0),),
    )
    assert apply_rules({"rushing_yards": 99}, rules).total_points == 0.0
    assert apply_rules({"rushing_yards": 150}, rules).total_points == 50.0


def test_threshold_max_caps_per_unit_window() -> None:
    # Per-unit rule capped between 100 and 200 yards
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule(
                "rushing",
                "rushing_yards",
                points_per_unit=1.0,
                threshold_min=100.0,
                threshold_max=200.0,
            ),
        ),
    )
    assert apply_rules({"rushing_yards": 250}, rules).total_points == 100.0
    assert apply_rules({"rushing_yards": 175}, rules).total_points == 75.0


def test_flat_bonus_requires_stat_to_be_reported() -> None:
    # An absent stat must NOT trigger a flat bonus, even if no threshold
    # is set — otherwise every player would get every defense bonus.
    rules = ScoringRules(
        season_id=1,
        rules=(_rule("misc", "appearance", flat_points=1.0),),
    )
    assert apply_rules({}, rules).total_points == 0.0
    assert apply_rules({"appearance": 1}, rules).total_points == 1.0
    # Even reporting 0 explicitly triggers the bonus (no threshold)
    assert apply_rules({"appearance": 0}, rules).total_points == 1.0


def test_flat_bonus_threshold_max_excludes_above() -> None:
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule(
                "misc",
                "x",
                flat_points=5.0,
                threshold_min=1.0,
                threshold_max=10.0,
            ),
        ),
    )
    assert apply_rules({"x": 0}, rules).total_points == 0.0
    assert apply_rules({"x": 1}, rules).total_points == 5.0
    assert apply_rules({"x": 10}, rules).total_points == 5.0
    assert apply_rules({"x": 11}, rules).total_points == 0.0


def test_categories_aggregate_independently() -> None:
    # Two rules with the same stat_key but different categories sum into
    # both buckets — handy when the same stat contributes to "passing"
    # tallies AND a separate "bonus" category in some leagues.
    rules = ScoringRules(
        season_id=1,
        rules=(
            _rule("passing", "passing_tds", points_per_unit=4.0),
            _rule("bonus", "passing_tds", points_per_unit=1.0),
        ),
    )
    result = apply_rules({"passing_tds": 2}, rules)
    assert result.breakdown == {"passing": 8.0, "bonus": 2.0}
    assert result.total_points == 10.0


def test_result_is_immutable_dataclass() -> None:
    result = apply_rules({"passing_tds": 1}, STD_PPR_RULES)
    assert isinstance(result, ScoredResult)
    with pytest.raises(AttributeError):
        result.total_points = 999  # type: ignore[misc]


def test_rules_object_is_hashable() -> None:
    # frozen dataclasses with tuple fields should be hashable, enabling
    # cache keys downstream.
    assert hash(STD_PPR_RULES) == hash(STD_PPR_RULES)


def test_doc_example_repl_session() -> None:
    """The "Done when" criterion from docs/09_ROADMAP.md M3."""
    result = apply_rules({"passing_yards": 312, "passing_tds": 2}, STD_PPR_RULES)
    # 312/25 = 12.48; 2 TDs = 8; 300+ bonus = 3 → 23.48
    assert result.total_points == 23.48
    assert result.breakdown == {"passing": 23.48}
