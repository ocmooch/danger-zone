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


def test_raiders_relocation_by_year() -> None:
    # Blank nfl_team, recovered from the "Raiders" nickname, then
    # relocation-adjusted: OAK through 2019, LV from 2020.
    p = _player(name_full="Raiders", nfl_team=None)
    assert resolve_def_team_abbrev(p, 2019) == "OAK"
    assert resolve_def_team_abbrev(p, 2020) == "LV"


def test_chargers_relocation_by_year() -> None:
    # Stored as the current LAC; SD through 2016, LAC from 2017.
    p = _player(name_full="Los Angeles Chargers", nfl_team="LAC")
    assert resolve_def_team_abbrev(p, 2016) == "SD"
    assert resolve_def_team_abbrev(p, 2017) == "LAC"


def test_rams_relocation_by_year() -> None:
    p = _player(name_full="Los Angeles Rams", nfl_team="LA")
    assert resolve_def_team_abbrev(p, 2015) == "STL"
    assert resolve_def_team_abbrev(p, 2016) == "LA"


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
