"""Scoring-rules loader.

Reads the league's scoring rules from a CSV-style export of the NFL.com
``/league/{LID}/settings`` page and produces:

1. A ``LeagueSettings`` value object — league ID, season year, ordered
   ``ScoringRule`` list — that the engine can score against immediately.
2. Side-effecting DB upserts: ``leagues`` / ``seasons`` / ``scoring_rules``
   rows for the parsed (league_id, season_year).

The CSV format we accept is **not** a tidy ``category,stat_key,...``
table; it's the raw text NFL.com renders, exported as alternating
``key`` / ``value`` lines with section headers ("Offense", "Kicking",
"Defense / Special Teams", "Other"). Example::

    League Name:
    The Danger Zone
    League ID:
    36271
    ...
    Trade Deadline:
    "November 21, 2025"
    ...
    Scoring Settings
    Offense
    Passing Yards:
    1 point per 25 yards
    Passing Touchdowns:
    4 points
    300-399 Passing Yards Bonus:
    1 point
    ...

The M5 NFL.com crawler will eventually scrape this same data directly; for
M4 we accept it via CSV so the engine has real rules to score against
before the HTML scraper is built.

Stat-key decisions documented inline (see also ``docs/05_SCORING_ENGINE.md``):

* ``2-Point Conversions: 2 points`` → three engine rules
  (``passing_2pt_conversions``, ``rushing_2pt_conversions``,
  ``receiving_2pt_conversions``), each worth +2. NFL.com awards +2 to the
  player who scored the conversion regardless of source play type.
* ``40+ Passing Yard TD Bonus`` and ``50+ Passing Yards TD Bonus`` STACK
  (a 55-yard TD earns both). Two separate per-unit rules counting TDs in
  each bracket; no ``threshold_max``.
* ``300-399 Passing Yards Bonus`` and ``400+ Passing Yards Bonus`` do NOT
  stack — encoded as mutually-exclusive ranges via ``threshold_min`` +
  ``threshold_max``.
* ``Kickoff and Punt Return Touchdowns`` appears in both ``Offense`` and
  ``Defense / Special Teams`` sections. Both map to
  ``special_teams_tds``, distinguished by ``category=`` (``misc`` vs
  ``defense``) so the per-category breakdown reflects which side accrued
  the points.
* Yards-allowed brackets (``Less than 100 Total Yards Allowed`` etc.) use
  a new ``total_yards_allowed`` stat key with range gating, mirroring
  ``points_allowed``. The corresponding stat must be produced upstream
  from team-defense rollups (M5/M7).
"""

from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime

# Path is used as a runtime type for typer-decorated CLI entry-point callers
# that import via get_type_hints; keep it as an eager import.
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.upsert import UpsertCounts, upsert
from ff_pipeline.scoring.rules import ScoringRule

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Parsed value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedRule:
    """A scoring rule plus the raw text it was parsed from.

    ``raw_text`` flows into ``scoring_rules.raw_text`` so a human can audit
    "did we interpret this NFL.com line correctly?" without re-reading the
    CSV.
    """

    rule: ScoringRule
    raw_text: str


@dataclass(frozen=True, slots=True)
class LeagueSettings:
    """The complete output of parsing one settings export."""

    league_id: str
    league_name: str | None
    season_year: int
    rules: tuple[ParsedRule, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def parse_settings_csv(path: Path) -> LeagueSettings:
    """Read ``path`` and return a fully-parsed ``LeagueSettings``.

    Raises ``ScoringParseError`` if mandatory fields (league ID, season
    year, at least one scoring rule) can't be extracted.
    """

    raw_lines = _read_csv_as_lines(path)
    pairs = _to_key_value_pairs(raw_lines)
    metadata = _extract_metadata(pairs)
    scoring_lines = _extract_scoring_pairs(pairs)
    rules = _parse_scoring_pairs(scoring_lines)

    if not rules:
        raise ScoringParseError(f"No scoring rules parsed from {path}")

    return LeagueSettings(
        league_id=metadata.league_id,
        league_name=metadata.league_name,
        season_year=metadata.season_year,
        rules=tuple(rules),
    )


def apply_settings_to_db(
    session: Session,
    settings: LeagueSettings,
    *,
    source_path: Path | None = None,
    fixtures_dir: Path | None = None,
) -> UpsertCounts:
    """Upsert league + season rows, then upsert scoring rules.

    Caller's responsibility to ``session.commit()``. If ``source_path`` and
    ``fixtures_dir`` are both provided, the CSV is copied into the
    fixtures directory (named ``{league_id}_{season_year}.csv``) for the
    M9 scoring verifier to consume later.
    """

    from ff_pipeline.repository.models import League, Season
    from ff_pipeline.repository.models import ScoringRule as ScoringRuleModel

    upsert(
        session,
        League,
        [{"league_id": settings.league_id, "name": settings.league_name, "platform": "nfl_com"}],
        conflict_cols=("league_id",),
    )

    upsert(
        session,
        Season,
        [{"league_id": settings.league_id, "year": settings.season_year}],
        conflict_cols=("league_id", "year"),
    )
    session.flush()

    season_id = _resolve_season_id(session, settings.league_id, settings.season_year)

    rule_rows = [
        {
            "season_id": season_id,
            "category": pr.rule.category,
            "stat_key": pr.rule.stat_key,
            "points_per_unit": pr.rule.points_per_unit,
            "unit_size": pr.rule.unit_size,
            "threshold_min": pr.rule.threshold_min,
            "threshold_max": pr.rule.threshold_max,
            "flat_points": pr.rule.flat_points,
            "raw_text": pr.raw_text,
        }
        for pr in settings.rules
    ]
    counts = upsert(
        session,
        ScoringRuleModel,
        rule_rows,
        conflict_cols=("season_id", "category", "stat_key", "threshold_min"),
    )

    if source_path and fixtures_dir:
        _preserve_fixture(source_path, fixtures_dir, settings)

    log.info(
        "Loaded scoring rules",
        league_id=settings.league_id,
        season_year=settings.season_year,
        rules_added=counts.rows_added,
        rules_updated=counts.rows_updated,
    )
    return counts


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScoringParseError(RuntimeError):
    """Raised when the settings export can't be parsed into rules."""


# ---------------------------------------------------------------------------
# Internal: format helpers
# ---------------------------------------------------------------------------


_SECTION_HEADERS = frozenset(
    {
        "scoring settings",
        "offense",
        "kicking",
        "defense / special teams",
        "other",
        # Pre-scoring sections we just skip.
        "league settings",
        "starting roster positions & roster limits",
        "keeper settings",
    }
)


@dataclass(frozen=True, slots=True)
class _Metadata:
    league_id: str
    league_name: str | None
    season_year: int


def _read_csv_as_lines(path: Path) -> list[str]:
    # The "CSV" is one cell per row; csv.reader handles any future
    # quoting (the NFL.com export wraps multi-word values in quotes).
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        return [(row[0] if row else "").strip() for row in reader]


def _to_key_value_pairs(lines: list[str]) -> list[tuple[str, str]]:
    """Walk lines and emit (key, value) pairs via strict alternation.

    NFL.com's settings export is alternating key/value lines from top to
    bottom. The first metadata block uses trailing-colon keys
    (``"League Name:"`` → ``"The Danger Zone"``); the rest of the file
    drops the colons. We strip any trailing colon and just count off
    pairs, resetting whenever we hit a recognized section header — which
    interrupts the pairing and does not consume a value of its own.
    """

    pairs: list[tuple[str, str]] = []
    pending_key: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        norm = line.rstrip(":").strip().lower()
        if norm in _SECTION_HEADERS:
            pairs.append((line.rstrip(":").strip(), ""))
            pending_key = None
            continue
        if pending_key is None:
            pending_key = line.rstrip(":").strip()
        else:
            pairs.append((pending_key, line))
            pending_key = None
    if pending_key is not None:
        pairs.append((pending_key, ""))
    return pairs


def _extract_metadata(pairs: list[tuple[str, str]]) -> _Metadata:
    by_key = {k: v for k, v in pairs if v}
    league_id = by_key.get("League ID")
    if not league_id:
        raise ScoringParseError("Could not find 'League ID:' in settings export")
    league_name = by_key.get("League Name") or None
    season_year = _infer_season_year(by_key)
    return _Metadata(
        league_id=league_id.strip(),
        league_name=league_name.strip() if league_name else None,
        season_year=season_year,
    )


def _infer_season_year(by_key: dict[str, str]) -> int:
    """Pull the season year from any date-bearing field we can find.

    NFL.com's export doesn't have a dedicated season-year cell, but the
    trade deadline ("November 21, 2025") and similar fields name the
    season directly. We try a couple of candidates and fall back to the
    current calendar year only if nothing matches — that fallback would
    be wrong during the off-season, so we log a warning when it triggers.
    """

    candidates = (
        by_key.get("Trade Deadline"),
        by_key.get("Start of Season"),
        by_key.get("Season"),
        by_key.get("Season Year"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        year = _year_from_text(candidate)
        if year is not None:
            return year
    fallback = datetime.now().year
    log.warning(
        "Could not infer season year from settings export; using current year",
        fallback_year=fallback,
    )
    return fallback


_YEAR_RE = re.compile(r"\b(19[89]\d|20\d{2}|21\d{2})\b")


def _year_from_text(text: str) -> int | None:
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def _extract_scoring_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Return only the (section, key, value) triples inside Scoring Settings.

    Walks the linear pairs list and tracks the current section header.
    Anything before "Scoring Settings" is dropped; once "Scoring Settings"
    is seen, every following key:value with a value is tagged with the
    most-recent subsection header (Offense / Kicking / Defense / Other).
    """

    out: list[tuple[str, str, str]] = []
    section: str | None = None
    in_scoring = False
    for key, value in pairs:
        norm = key.strip().lower()
        if norm == "scoring settings":
            in_scoring = True
            continue
        if not in_scoring:
            continue
        if value == "" and norm in _SECTION_HEADERS:
            # Subsection header inside Scoring Settings — track it.
            section = key.strip()
            continue
        if section is not None and section.lower() == "other":
            # "Other" holds toggles ("Use Fractional Pts"), not rules.
            continue
        if not value or section is None:
            continue
        out.append((section, key.strip(), value.strip()))
    return out


# ---------------------------------------------------------------------------
# Internal: rule parsing
# ---------------------------------------------------------------------------


# NFL.com section -> engine category. "Offense" lines have category set
# per stat key (passing/rushing/receiving/misc), not at the section level.
_SECTION_TO_DEFENSE_CATEGORY = {
    "Defense / Special Teams": "defense",
    "Kicking": "kicking",
}


def _parse_scoring_pairs(triples: list[tuple[str, str, str]]) -> list[ParsedRule]:
    rules: list[ParsedRule] = []
    for section, label, value in triples:
        try:
            parsed = _parse_one_rule(section, label, value)
        except ScoringParseError as exc:
            log.warning(
                "Skipping unparseable scoring line",
                section=section,
                label=label,
                value=value,
                error=str(exc),
            )
            continue
        if parsed is None:
            log.info(
                "Unrecognized scoring line (no engine mapping)",
                section=section,
                label=label,
                value=value,
            )
            continue
        rules.extend(parsed)
    return rules


def _parse_one_rule(section: str, label: str, value: str) -> list[ParsedRule] | None:
    """Return one or more ParsedRule for the (section, label, value) triple.

    Returns None when the label isn't one we know how to map. Raises
    ScoringParseError when we know the label but couldn't parse the value
    (better to log + skip than guess silently).
    """

    raw_text = f"{label}: {value}"
    norm_label = _normalize_label(label)

    # Offense category resolution: each label tells us its category.
    if section == "Offense":
        spec = _OFFENSE_LABELS.get(norm_label)
        if spec is None:
            return None
        return _emit_rules_for_spec(spec, value, raw_text)

    if section == "Kicking":
        spec = _KICKING_LABELS.get(norm_label)
        if spec is None:
            return None
        return _emit_rules_for_spec(spec, value, raw_text)

    if section == "Defense / Special Teams":
        spec = _DEFENSE_LABELS.get(norm_label)
        if spec is None:
            return None
        return _emit_rules_for_spec(spec, value, raw_text)

    return None


def _normalize_label(label: str) -> str:
    """Lowercase + collapse whitespace; tolerate the ``Yard`` vs ``Yards`` typo.

    NFL.com is inconsistent ("40+ Passing Yard TD Bonus" vs "50+ Passing
    Yards TD Bonus"). Strip the singular/plural distinction so the mapping
    table can have one canonical key.
    """

    norm = re.sub(r"\s+", " ", label).strip().lower()
    norm = re.sub(r"\byards?\b", "yards", norm)
    return norm


# ---- Rule specs ------------------------------------------------------------
#
# A "spec" tells _emit_rules_for_spec how to translate the parsed numeric
# value (e.g. "1 point per 25 yards") into one or more ScoringRule rows.
# Five spec shapes are supported, modelled as discriminated tuples to keep
# the table compact and readable:
#
#   ("per_unit", category, stat_key, default_unit)
#       Value is "{N} point[s] per {U} yards" or "{N} point[s]". One rule.
#
#   ("per_unit_multi", (category, stat_key, default_unit), ...)
#       Same parse as "per_unit", emits one rule per (cat, key) tuple.
#       Used for "2-Point Conversions" → pass/rush/recv.
#
#   ("flat", category, stat_key, threshold_min, threshold_max)
#       Value is "{N} point[s]". Emits a flat-bonus rule with the given
#       window; threshold_max may be None for ">= threshold_min" rules.
#
#   ("per_unit_threshold", category, stat_key, threshold_min)
#       Long-TD bonuses: value is "{N} point[s]"; rule scores per stat
#       value (count of long TDs), no threshold_max so 40+ and 50+ stack.
#
# Every Offense label includes its category (passing/rushing/receiving/misc)
# directly. Kicking and Defense use a single shared category each.


_OFFENSE_LABELS: dict[str, tuple[Any, ...]] = {
    # Passing
    "passing yards": ("per_unit", "passing", "passing_yards", 25.0),
    "passing touchdowns": ("per_unit", "passing", "passing_tds", 1.0),
    "interceptions thrown": ("per_unit", "passing", "passing_interceptions", 1.0),
    "300-399 passing yards bonus": ("flat", "passing", "passing_yards", 300.0, 399.0),
    "400+ passing yards bonus": ("flat", "passing", "passing_yards", 400.0, None),
    "40+ passing yards td bonus": (
        "per_unit_threshold",
        "passing",
        "passing_yards_bonus_long_td_40",
        1.0,
    ),
    "50+ passing yards td bonus": (
        "per_unit_threshold",
        "passing",
        "passing_yards_bonus_long_td_50",
        1.0,
    ),
    # Rushing
    "rushing yards": ("per_unit", "rushing", "rushing_yards", 10.0),
    "rushing touchdowns": ("per_unit", "rushing", "rushing_tds", 1.0),
    "100-199 rushing yards bonus": ("flat", "rushing", "rushing_yards", 100.0, 199.0),
    "200+ rushing yards bonus": ("flat", "rushing", "rushing_yards", 200.0, None),
    "40+ rushing yards td bonus": (
        "per_unit_threshold",
        "rushing",
        "rushing_yards_bonus_long_td_40",
        1.0,
    ),
    "50+ rushing yards td bonus": (
        "per_unit_threshold",
        "rushing",
        "rushing_yards_bonus_long_td_50",
        1.0,
    ),
    # Receiving
    "receptions": ("per_unit", "receiving", "receptions", 1.0),
    "receiving yards": ("per_unit", "receiving", "receiving_yards", 10.0),
    "receiving touchdowns": ("per_unit", "receiving", "receiving_tds", 1.0),
    "100-199 receiving yards bonus": ("flat", "receiving", "receiving_yards", 100.0, 199.0),
    "200+ receiving yards bonus": ("flat", "receiving", "receiving_yards", 200.0, None),
    "40+ receiving yards td bonus": (
        "per_unit_threshold",
        "receiving",
        "receiving_yards_bonus_long_td_40",
        1.0,
    ),
    "50+ receiving yards td bonus": (
        "per_unit_threshold",
        "receiving",
        "receiving_yards_bonus_long_td_50",
        1.0,
    ),
    # Misc offense
    "kickoff and punt return touchdowns": ("per_unit", "misc", "special_teams_tds", 1.0),
    "fumbles lost": ("per_unit", "misc", "fumbles_lost", 1.0),
    "2-point conversions": (
        "per_unit_multi",
        (
            ("passing", "passing_2pt_conversions", 1.0),
            ("rushing", "rushing_2pt_conversions", 1.0),
            ("receiving", "receiving_2pt_conversions", 1.0),
        ),
    ),
}


_KICKING_LABELS: dict[str, tuple[Any, ...]] = {
    "pat made": ("per_unit", "kicking", "extra_point_made", 1.0),
    "fg made 0-19": ("per_unit", "kicking", "field_goal_made_0_19", 1.0),
    "fg made 20-29": ("per_unit", "kicking", "field_goal_made_20_29", 1.0),
    "fg made 30-39": ("per_unit", "kicking", "field_goal_made_30_39", 1.0),
    "fg made 40-49": ("per_unit", "kicking", "field_goal_made_40_49", 1.0),
    "fg made 50+": ("per_unit", "kicking", "field_goal_made_50_plus", 1.0),
}


_DEFENSE_LABELS: dict[str, tuple[Any, ...]] = {
    "sacks": ("per_unit", "defense", "sacks", 1.0),
    "interceptions": ("per_unit", "defense", "interceptions", 1.0),
    "fumbles recovered": ("per_unit", "defense", "fumbles_recovered", 1.0),
    "safeties": ("per_unit", "defense", "safeties", 1.0),
    "touchdowns": ("per_unit", "defense", "defensive_tds", 1.0),
    "kickoff and punt return touchdowns": ("per_unit", "defense", "special_teams_tds", 1.0),
    # Points allowed brackets.
    "points allowed 0": ("flat", "defense", "points_allowed", 0.0, 0.0),
    "points allowed 1-6": ("flat", "defense", "points_allowed", 1.0, 6.0),
    "points allowed 7-13": ("flat", "defense", "points_allowed", 7.0, 13.0),
    "points allowed 14-20": ("flat", "defense", "points_allowed", 14.0, 20.0),
    "points allowed 21-27": ("flat", "defense", "points_allowed", 21.0, 27.0),
    "points allowed 28-34": ("flat", "defense", "points_allowed", 28.0, 34.0),
    "points allowed 35+": ("flat", "defense", "points_allowed", 35.0, None),
    # Total-yards-allowed brackets — new stat-key family in M4 (see docstring).
    "less than 100 total yards allowed": (
        "flat",
        "defense",
        "total_yards_allowed",
        0.0,
        99.0,
    ),
    "100-199 yards allowed": ("flat", "defense", "total_yards_allowed", 100.0, 199.0),
    "200-299 yards allowed": ("flat", "defense", "total_yards_allowed", 200.0, 299.0),
    "300-399 yards allowed": ("flat", "defense", "total_yards_allowed", 300.0, 399.0),
    "400-449 yards allowed": ("flat", "defense", "total_yards_allowed", 400.0, 449.0),
    "450-499 yards allowed": ("flat", "defense", "total_yards_allowed", 450.0, 499.0),
    "500+ yards allowed": ("flat", "defense", "total_yards_allowed", 500.0, None),
}


def _emit_rules_for_spec(spec: tuple[Any, ...], value: str, raw_text: str) -> list[ParsedRule]:
    kind = spec[0]
    if kind == "per_unit":
        _, category, stat_key, default_unit = spec
        pts, unit = _parse_per_unit_value(value, default_unit)
        return [
            ParsedRule(
                rule=ScoringRule(
                    category=category,
                    stat_key=stat_key,
                    points_per_unit=pts,
                    unit_size=unit,
                    # 0.0 (not None) so the (season, cat, key, thresh_min)
                    # unique constraint matches across re-runs. The engine
                    # treats threshold_min=0 the same as no threshold for
                    # non-negative stats (which is every football stat).
                    threshold_min=0.0,
                ),
                raw_text=raw_text,
            )
        ]
    if kind == "per_unit_multi":
        _, targets = spec
        pts, unit = _parse_per_unit_value(value, 1.0)
        return [
            ParsedRule(
                rule=ScoringRule(
                    category=cat,
                    stat_key=key,
                    points_per_unit=pts,
                    unit_size=unit if unit is not None else default_unit,
                    threshold_min=0.0,
                ),
                raw_text=raw_text,
            )
            for (cat, key, default_unit) in targets
        ]
    if kind == "flat":
        _, category, stat_key, thresh_min, thresh_max = spec
        pts = _parse_flat_value(value)
        return [
            ParsedRule(
                rule=ScoringRule(
                    category=category,
                    stat_key=stat_key,
                    flat_points=pts,
                    threshold_min=thresh_min,
                    threshold_max=thresh_max,
                ),
                raw_text=raw_text,
            )
        ]
    if kind == "per_unit_threshold":
        _, category, stat_key, unit = spec
        pts = _parse_flat_value(value)
        return [
            ParsedRule(
                rule=ScoringRule(
                    category=category,
                    stat_key=stat_key,
                    points_per_unit=pts,
                    unit_size=unit,
                    threshold_min=0.0,
                ),
                raw_text=raw_text,
            )
        ]
    raise ScoringParseError(f"Unknown spec kind: {kind!r}")


_PER_UNIT_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*points?\s*(?:per\s+(\d+(?:\.\d+)?)\s+\w+)?\s*$",
    re.IGNORECASE,
)
_FLAT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*points?\s*$", re.IGNORECASE)


def _parse_per_unit_value(value: str, default_unit: float) -> tuple[float, float]:
    """Parse "1 point per 25 yards" or "4 points" → (points, unit_size)."""

    match = _PER_UNIT_RE.match(value)
    if not match:
        raise ScoringParseError(f"Could not parse per-unit value {value!r}")
    points = float(match.group(1))
    unit = float(match.group(2)) if match.group(2) else default_unit
    return points, unit


def _parse_flat_value(value: str) -> float:
    match = _FLAT_RE.match(value)
    if not match:
        raise ScoringParseError(f"Could not parse flat-points value {value!r}")
    return float(match.group(1))


# ---------------------------------------------------------------------------
# Internal: DB helpers
# ---------------------------------------------------------------------------


def _resolve_season_id(session: Session, league_id: str, year: int) -> int:
    from sqlalchemy import select

    from ff_pipeline.repository.models import Season

    stmt = select(Season.season_id).where(Season.league_id == league_id, Season.year == year)
    season_id = session.execute(stmt).scalar_one_or_none()
    if season_id is None:
        raise ScoringParseError(
            f"Season row for league_id={league_id!r}, year={year} unexpectedly missing"
        )
    return season_id


def _preserve_fixture(source_path: Path, fixtures_dir: Path, settings: LeagueSettings) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    target = fixtures_dir / f"{settings.league_id}_{settings.season_year}.csv"
    if target.resolve() == source_path.resolve():
        return  # source IS the fixture; nothing to copy
    shutil.copyfile(source_path, target)
    log.info("Copied scoring-rules fixture", source=str(source_path), target=str(target))


__all__ = [
    "LeagueSettings",
    "ParsedRule",
    "ScoringParseError",
    "apply_settings_to_db",
    "parse_settings_csv",
]
