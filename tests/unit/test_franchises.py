"""Unit tests for the DEF → nflverse-abbreviation resolver.

Pure function over a player-like object; no DB needed. Covers:

* abbreviation taken straight from ``nfl_team`` when present;
* nickname backfill when ``nfl_team`` is blank;
* relocation-aware abbreviations keyed by season year;
* unresolvable input returns ``None``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ff_pipeline.crawlers.nflverse.franchises import (
    historical_team_code,
    resolve_def_team_abbrev,
)


def _player(*, name_full: str, nfl_team: str | None) -> SimpleNamespace:
    return SimpleNamespace(player_id=1, name_full=name_full, nfl_team=nfl_team)


def test_uses_stored_abbrev_when_present() -> None:
    p = _player(name_full="San Francisco 49ers", nfl_team="SF")
    assert resolve_def_team_abbrev(p, 2024) == "SF"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Cowboys", "DAL"),
        ("Jets", "NYJ"),
        ("Panthers", "CAR"),
        ("New York Giants", "NYG"),
    ],
)
def test_backfills_blank_nfl_team_from_nickname(name: str, expected: str) -> None:
    p = _player(name_full=name, nfl_team=None)
    assert resolve_def_team_abbrev(p, 2024) == expected


def test_relocated_def_always_resolves_to_current_code() -> None:
    # nflverse keys every season under the current code, so the ingest index
    # must use the current code regardless of season — pre-move years included.
    # (The pre-move display code lives in historical_team_code, not here.)
    raiders = _player(name_full="Raiders", nfl_team=None)
    assert resolve_def_team_abbrev(raiders, 2019) == "LV"
    assert resolve_def_team_abbrev(raiders, 2020) == "LV"

    chargers = _player(name_full="Los Angeles Chargers", nfl_team="LAC")
    assert resolve_def_team_abbrev(chargers, 2016) == "LAC"
    assert resolve_def_team_abbrev(chargers, 2017) == "LAC"

    rams = _player(name_full="Los Angeles Rams", nfl_team="LA")
    assert resolve_def_team_abbrev(rams, 2015) == "LA"
    assert resolve_def_team_abbrev(rams, 2016) == "LA"


def test_rams_stored_lar_folds_to_nflverse_la() -> None:
    # Our players row carries "LAR", but nflverse codes the Rams "LA" in every
    # season — the stored-abbrev alias folds LAR→LA so the DEF still matches.
    p = _player(name_full="Los Angeles Rams", nfl_team="LAR")
    assert resolve_def_team_abbrev(p, 2024) == "LA"
    assert resolve_def_team_abbrev(p, 2016) == "LA"


def test_unresolvable_returns_none() -> None:
    p = _player(name_full="", nfl_team=None)
    assert resolve_def_team_abbrev(p, 2024) is None


@pytest.mark.parametrize(
    ("current", "season", "expected"),
    [
        ("LV", 2019, "OAK"),  # Raiders moved to Las Vegas in 2020
        ("LV", 2020, "LV"),
        ("LAC", 2016, "SD"),  # Chargers moved to LA in 2017
        ("LAC", 2017, "LAC"),
        ("LA", 2015, "STL"),  # Rams moved to LA in 2016
        ("LA", 2016, "LA"),
        ("SF", 2010, "SF"),  # non-relocated → passthrough
        ("lv", 2019, "OAK"),  # case-insensitive
    ],
)
def test_historical_team_code(current: str, season: int, expected: str) -> None:
    assert historical_team_code(current, season) == expected
