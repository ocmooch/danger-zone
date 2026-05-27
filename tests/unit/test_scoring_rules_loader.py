"""Unit tests for ``ff_pipeline.scoring.scraper``.

The loader has two halves:

1. ``parse_settings_csv`` — pure text → ``LeagueSettings``. Tested against
   the user's real ``.project-src/dz-rules.csv`` to lock in the parse,
   and against hand-crafted edge cases for the key/value alternation,
   long-TD stacking, range-bonus mutual exclusion, and the 2-pt fan-out.
2. ``apply_settings_to_db`` — upserts into ``leagues`` / ``seasons`` /
   ``scoring_rules`` and is idempotent on re-runs.

The output of (1) is also fed through the scoring engine to verify the
end-to-end story: real rules + real-shaped stats → correct points.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import League, ScoringRule, Season
from ff_pipeline.scoring.engine import apply_rules
from ff_pipeline.scoring.rules import ScoringRules
from ff_pipeline.scoring.scraper import (
    ScoringParseError,
    apply_settings_to_db,
    parse_settings_csv,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DZ_RULES_CSV = PROJECT_ROOT / ".project-src" / "dz-rules.csv"


# ---------------------------------------------------------------------------
# Real-CSV parse
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dz_settings():  # type: ignore[no-untyped-def]
    return parse_settings_csv(DZ_RULES_CSV)


def test_real_csv_extracts_metadata(dz_settings) -> None:  # type: ignore[no-untyped-def]
    assert dz_settings.league_id == "36271"
    assert dz_settings.league_name == "The Danger Zone"
    assert dz_settings.season_year == 2025


def test_real_csv_emits_expected_rule_count(dz_settings) -> None:  # type: ignore[no-untyped-def]
    # 51 = 22 offense (incl. 3 2pt fanned out) + 6 kicking + 23 defense
    assert len(dz_settings.rules) == 51


def test_real_csv_2pt_conversion_fans_out_to_three_stat_keys(dz_settings) -> None:  # type: ignore[no-untyped-def]
    keys = {pr.rule.stat_key for pr in dz_settings.rules if "2pt" in pr.rule.stat_key}
    assert keys == {
        "passing_2pt_conversions",
        "rushing_2pt_conversions",
        "receiving_2pt_conversions",
    }


def test_real_csv_long_td_bonuses_emit_both_40_and_50(dz_settings) -> None:  # type: ignore[no-untyped-def]
    long_tds = sorted(
        pr.rule.stat_key
        for pr in dz_settings.rules
        if pr.rule.stat_key.endswith(("_long_td_40", "_long_td_50"))
    )
    # 3 categories (pass/rush/recv) x 2 tiers (40+, 50+) = 6
    assert long_tds == [
        "passing_yards_bonus_long_td_40",
        "passing_yards_bonus_long_td_50",
        "receiving_yards_bonus_long_td_40",
        "receiving_yards_bonus_long_td_50",
        "rushing_yards_bonus_long_td_40",
        "rushing_yards_bonus_long_td_50",
    ]


def test_real_csv_range_bonuses_are_mutually_exclusive(dz_settings) -> None:  # type: ignore[no-untyped-def]
    rules = ScoringRules(season_id=1, rules=tuple(pr.rule for pr in dz_settings.rules))
    # 350-yard passing day: 1pt bonus (300-399 tier) but NOT the 400+ tier
    r = apply_rules({"passing_yards": 350}, rules)
    assert r.total_points == pytest.approx(15.0)  # 350/25 + 1 = 14 + 1
    # 400-yard day: NO 300-399 bonus, only the 400+ (3 pts)
    r = apply_rules({"passing_yards": 400}, rules)
    assert r.total_points == pytest.approx(19.0)  # 400/25 + 3 = 16 + 3


def test_real_csv_long_td_bonuses_stack(dz_settings) -> None:  # type: ignore[no-untyped-def]
    rules = ScoringRules(season_id=1, rules=tuple(pr.rule for pr in dz_settings.rules))
    # 1 TD that's 50+ yards triggers BOTH the 40+ (+1) AND 50+ (+3) rules → +4
    r = apply_rules(
        {
            "passing_tds": 1,
            "passing_yards_bonus_long_td_40": 1,
            "passing_yards_bonus_long_td_50": 1,
        },
        rules,
    )
    # 4 (TD) + 1 (40+) + 3 (50+) = 8
    assert r.total_points == pytest.approx(8.0)


def test_real_csv_total_yards_allowed_brackets_present(dz_settings) -> None:  # type: ignore[no-untyped-def]
    yards_rules = [pr.rule for pr in dz_settings.rules if pr.rule.stat_key == "total_yards_allowed"]
    assert len(yards_rules) == 7  # 0-99, 100-199, 200-299, 300-399, 400-449, 450-499, 500+
    rules = ScoringRules(season_id=1, rules=tuple(pr.rule for pr in dz_settings.rules))
    # 250 yards allowed -> 200-299 bracket = +4
    r = apply_rules({"total_yards_allowed": 250}, rules)
    assert r.breakdown["defense"] == pytest.approx(4.0)


def test_real_csv_kickoff_punt_return_td_in_both_categories(dz_settings) -> None:  # type: ignore[no-untyped-def]
    rules = ScoringRules(season_id=1, rules=tuple(pr.rule for pr in dz_settings.rules))
    r = apply_rules({"special_teams_tds": 1}, rules)
    # Two rules both fire (one in misc for the offense, one in defense for DST)
    assert r.breakdown.get("misc") == pytest.approx(6.0)
    assert r.breakdown.get("defense") == pytest.approx(6.0)
    assert r.total_points == pytest.approx(12.0)


def test_real_csv_negative_rushing_yards_subtract_points(dz_settings) -> None:  # type: ignore[no-untyped-def]
    # Locks in M4's negative-stat fix: a -3 yard carry must score -0.3
    # under the parsed dz-rules (NOT 0 — which is what the engine would
    # have returned before the threshold_min>0 guard was added).
    rules = ScoringRules(season_id=1, rules=tuple(pr.rule for pr in dz_settings.rules))
    r = apply_rules({"rushing_yards": -3}, rules)
    assert r.total_points == pytest.approx(-0.3)


def test_real_csv_raw_text_round_trips(dz_settings) -> None:  # type: ignore[no-untyped-def]
    # Picking one well-known line. The raw_text must include both label
    # and value so a human can audit the rule against the original page.
    raws = [
        pr.raw_text
        for pr in dz_settings.rules
        if pr.rule.stat_key == "passing_yards" and pr.rule.flat_points is None
    ]
    assert raws == ["Passing Yards: 1 point per 25 yards"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_missing_league_id_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("Scoring Settings\nOffense\nPassing Yards:\n1 point per 25 yards\n")
    with pytest.raises(ScoringParseError, match="League ID"):
        parse_settings_csv(bad)


def test_unparseable_per_unit_value_is_skipped(tmp_path: Path) -> None:
    csv = tmp_path / "weird.csv"
    csv.write_text(
        "League Name:\nLG\n"
        "League ID:\n1\n"
        "Trade Deadline:\nNovember 1, 2025\n"
        "Scoring Settings\nOffense\n"
        "Passing Yards:\nentirely unparseable\n"
        "Passing Touchdowns:\n4 points\n"
    )
    settings = parse_settings_csv(csv)
    stat_keys = [pr.rule.stat_key for pr in settings.rules]
    assert "passing_tds" in stat_keys
    assert "passing_yards" not in stat_keys


# ---------------------------------------------------------------------------
# DB write path
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def test_apply_settings_creates_league_season_rules(session: Session) -> None:
    parsed = parse_settings_csv(DZ_RULES_CSV)
    counts = apply_settings_to_db(session, parsed)
    session.commit()

    assert counts.rows_added == len(parsed.rules)
    assert counts.rows_updated == 0

    league = session.execute(
        select(League).where(League.league_id == parsed.league_id)
    ).scalar_one()
    assert league.name == "The Danger Zone"

    season = session.execute(
        select(Season).where(Season.league_id == parsed.league_id, Season.year == 2025)
    ).scalar_one()
    rule_count = session.execute(
        select(ScoringRule).where(ScoringRule.season_id == season.season_id)
    ).all()
    assert len(rule_count) == len(parsed.rules)


def test_apply_settings_is_idempotent(session: Session) -> None:
    parsed = parse_settings_csv(DZ_RULES_CSV)
    apply_settings_to_db(session, parsed)
    session.commit()
    second = apply_settings_to_db(session, parsed)
    session.commit()

    assert second.rows_added == 0
    assert second.rows_updated == len(parsed.rules)


def test_apply_settings_copies_fixture(tmp_path: Path, session: Session) -> None:
    parsed = parse_settings_csv(DZ_RULES_CSV)
    fixtures_dir = tmp_path / "scoring_rules_fixtures"
    apply_settings_to_db(
        session,
        parsed,
        source_path=DZ_RULES_CSV,
        fixtures_dir=fixtures_dir,
    )
    session.commit()
    target = fixtures_dir / "36271_2025.csv"
    assert target.exists()
    assert target.read_text() == DZ_RULES_CSV.read_text()
