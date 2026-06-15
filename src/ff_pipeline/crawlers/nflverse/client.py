"""nflverse data-source wrapper.

Wraps ``nflreadpy`` (which returns Polars DataFrames) and produces small,
typed Python dataclasses keyed by the canonical NFL ``gsis_id``. The repo
upserter takes those dataclasses; no Polars frame escapes this module.

The ``NflverseSource`` indirection is the test seam: tests pass a
``LocalParquetSource`` pointing at a committed fixture so the suite never
hits the network. Production code passes ``LiveNflverseSource()`` (the
default), which calls ``nflreadpy.load_*`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

import polars as pl

from ff_pipeline.crawlers.nflverse.stat_keys import (
    expected_nflverse_columns,
    project_stats,
)
from ff_pipeline.crawlers.nflverse.team_defense import (
    TeamDefenseStat,
    build_team_defense_stats,
    expected_team_columns,
)
from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NflversePlayerStat:
    """One nflverse weekly stat row, projected onto engine stat keys."""

    gsis_id: str
    player_display_name: str | None
    position: str | None
    nfl_team: str | None
    season_year: int
    week: int
    season_type: str
    nfl_opponent: str | None
    stats: dict[str, float]


@dataclass(frozen=True, slots=True)
class NflversePlayerMeta:
    """One row from ``load_players()`` — used to upsert into ``players``."""

    gsis_id: str
    name_full: str
    name_first: str | None
    name_last: str | None
    position: str | None
    nfl_team: str | None
    birth_date: date | None
    rookie_year: int | None
    last_season: int | None
    espn_id: str | None
    status: str | None


# ---------------------------------------------------------------------------
# Source seam
# ---------------------------------------------------------------------------


class NflverseSource(Protocol):
    """Test seam between nflreadpy and our client.

    Both implementations return Polars frames; downstream projection is
    identical regardless of where the data came from.
    """

    def load_player_stats(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def load_players(self) -> pl.DataFrame: ...
    def load_rosters(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def load_schedules(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def load_team_stats(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def load_injuries(self, seasons: Sequence[int]) -> pl.DataFrame: ...
    def load_pbp(self, seasons: Sequence[int]) -> pl.DataFrame: ...


class LiveNflverseSource:
    """The production source — talks to nflreadpy over the network."""

    def load_player_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl  # local import keeps test paths cheap

        frame: pl.DataFrame = nfl.load_player_stats(seasons=list(seasons))
        return frame

    def load_players(self) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_players()
        return frame

    def load_rosters(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_rosters(seasons=list(seasons))
        return frame

    def load_schedules(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_schedules(seasons=list(seasons))
        return frame

    def load_team_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_team_stats(seasons=list(seasons))
        return frame

    def load_injuries(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_injuries(seasons=list(seasons))
        return frame

    def load_pbp(self, seasons: Sequence[int]) -> pl.DataFrame:
        import nflreadpy as nfl

        frame: pl.DataFrame = nfl.load_pbp(seasons=list(seasons))
        return frame


@dataclass(frozen=True, slots=True)
class LocalParquetSource:
    """Reads pre-downloaded parquet files from a directory.

    Expected filenames (one per nflreadpy function):

    * ``player_stats_{year}.parquet``
    * ``players.parquet``
    * ``rosters_{year}.parquet``
    * ``schedules_{year}.parquet``
    * ``team_stats_{year}.parquet``
    * ``pbp_{year}.parquet``

    Missing files raise — tests should fail loudly if a fixture is absent.
    """

    directory: Path

    def load_player_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"player_stats_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def load_players(self) -> pl.DataFrame:
        return self._read("players.parquet")

    def load_rosters(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"rosters_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def load_schedules(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"schedules_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def load_team_stats(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"team_stats_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def load_injuries(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"injuries_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def load_pbp(self, seasons: Sequence[int]) -> pl.DataFrame:
        frames = [self._read(f"pbp_{y}.parquet") for y in seasons]
        return pl.concat(frames) if len(frames) > 1 else frames[0]

    def _read(self, filename: str) -> pl.DataFrame:
        path = self.directory / filename
        if not path.exists():
            raise FileNotFoundError(f"nflverse fixture missing: {path}")
        return pl.read_parquet(path)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NflverseClient:
    """High-level reader: returns lists of typed dataclasses.

    Statefulness is limited to a one-time warning if nflverse appears to
    have renamed columns we project against — the warning fires once per
    process so noisy logs aren't generated per batch.
    """

    def __init__(self, source: NflverseSource | None = None) -> None:
        self._source: NflverseSource = source or LiveNflverseSource()
        self._missing_columns_warned: set[str] = set()

    # ----- player_stats -----

    def player_stats(self, seasons: Sequence[int]) -> list[NflversePlayerStat]:
        df = self._source.load_player_stats(seasons)
        self._check_columns(df, "player_stats")

        out: list[NflversePlayerStat] = []
        for row in df.iter_rows(named=True):
            gsis = row.get("player_id")
            if not gsis:
                # nflverse occasionally emits team-aggregate rows; skip.
                continue
            stats = project_stats(row)
            out.append(
                NflversePlayerStat(
                    gsis_id=str(gsis),
                    player_display_name=_opt_str(row.get("player_display_name")),
                    position=_opt_str(row.get("position")),
                    nfl_team=_opt_str(row.get("team")),
                    season_year=int(row["season"]),
                    week=int(row["week"]),
                    season_type=str(row.get("season_type") or "REG"),
                    nfl_opponent=_opt_str(row.get("opponent_team")),
                    stats=stats,
                )
            )
        log.info(
            "Loaded nflverse player_stats",
            seasons=list(seasons),
            row_count=len(out),
        )
        return out

    # ----- team_defense -----

    def team_defense_stats(self, seasons: Sequence[int]) -> list[TeamDefenseStat]:
        """Roll up team-level frames into per-team DST stat dicts.

        Reads ``load_team_stats`` (counting events + offensive yardage) and
        ``load_schedules`` (scores + opponent identity) and combines them
        via :func:`build_team_defense_stats`. The returned stats are keyed
        to the engine's defense vocabulary, ready for the scorer.
        """

        team_df = self._source.load_team_stats(seasons)
        self._check_team_columns(team_df)
        schedule_df = self._source.load_schedules(seasons)
        load_pbp = getattr(self._source, "load_pbp", None)
        play_by_play_df = load_pbp(seasons) if callable(load_pbp) else pl.DataFrame()

        out = build_team_defense_stats(
            team_rows=team_df.iter_rows(named=True),
            schedule_rows=schedule_df.iter_rows(named=True),
            play_by_play_rows=play_by_play_df.iter_rows(named=True),
        )
        log.info(
            "Built nflverse team-defense stats",
            seasons=list(seasons),
            row_count=len(out),
        )
        return out

    # ----- players -----

    def players(self) -> list[NflversePlayerMeta]:
        df = self._source.load_players()
        out: list[NflversePlayerMeta] = []
        for row in df.iter_rows(named=True):
            gsis = row.get("gsis_id")
            if not gsis:
                continue
            out.append(
                NflversePlayerMeta(
                    gsis_id=str(gsis),
                    name_full=_first_present(
                        row.get("display_name"),
                        row.get("football_name"),
                        row.get("short_name"),
                    )
                    or str(gsis),
                    name_first=_opt_str(row.get("first_name")),
                    name_last=_opt_str(row.get("last_name")),
                    position=_opt_str(row.get("position")),
                    nfl_team=_opt_str(row.get("latest_team")),
                    birth_date=_parse_date(row.get("birth_date")),
                    rookie_year=_opt_int(row.get("rookie_season")),
                    last_season=_opt_int(row.get("last_season")),
                    espn_id=_opt_str(row.get("espn_id")),
                    status=_opt_str(row.get("status")),
                )
            )
        log.info("Loaded nflverse players", row_count=len(out))
        return out

    # ----- internals -----

    def _check_columns(self, df: pl.DataFrame, source_label: str) -> None:
        present = set(df.columns)
        missing = expected_nflverse_columns() - present
        new_missing = missing - self._missing_columns_warned
        if new_missing:
            log.warning(
                "nflverse columns expected by projection are absent",
                source=source_label,
                missing=sorted(new_missing),
            )
            self._missing_columns_warned.update(new_missing)

    def _check_team_columns(self, df: pl.DataFrame) -> None:
        """Warn once if a team-defense column the rollup reads is absent.

        Each engine defense key maps to a *list* of candidate columns, so a
        single absent candidate isn't necessarily a problem — but a column
        we expected to find disappearing is worth surfacing, since the
        ``def_*`` family has been renamed across nflverse versions.
        """
        present = set(df.columns)
        missing = expected_team_columns() - present
        new_missing = missing - self._missing_columns_warned
        if new_missing:
            log.warning(
                "nflverse team-stat columns expected by team-defense rollup are absent",
                source="team_stats",
                missing=sorted(new_missing),
            )
            self._missing_columns_warned.update(new_missing)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        # nflverse uses ISO-8601 (YYYY-MM-DD) for birth_date.
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _first_present(*values: object) -> str | None:
    for v in values:
        s = _opt_str(v)
        if s:
            return s
    return None


__all__ = [
    "LiveNflverseSource",
    "LocalParquetSource",
    "NflverseClient",
    "NflversePlayerMeta",
    "NflversePlayerStat",
    "NflverseSource",
    "TeamDefenseStat",
]
