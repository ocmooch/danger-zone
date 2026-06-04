"""Pydantic response models for the FastAPI read service.

The API contract (``docs/06_API_CONTRACT.md``) requires every successful
response to be a JSON envelope:

```
{"data": {...}, "meta": {"last_updated": "...", "source": "...", "pipeline_run_id": N}}
```

We model the envelope as a generic ``Envelope[T]`` and define one
``BaseModel`` per resource. All entity models set
``model_config = ConfigDict(from_attributes=True)`` so we can construct
them directly from SQLAlchemy ORM rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Meta(BaseModel):
    """Audit metadata attached to every API response.

    * ``last_updated`` — ISO timestamp of the underlying data (the
      entity's ``updated_at`` for single-resource endpoints, max across
      collection rows otherwise).
    * ``source`` — which crawler / pipeline stage produced the data.
    * ``pipeline_run_id`` — most recent successful pipeline run touching
      the relevant data, or null if the pipeline has never run.
    """

    last_updated: datetime | None = None
    source: str = "pipeline"
    pipeline_run_id: int | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


class ErrorBody(BaseModel):
    error: str
    detail: str
    status: int


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"


class SourceHealthSummary(BaseModel):
    source: str
    status: str
    rows_added: int | None = None
    rows_updated: int | None = None
    parse_failures: int | None = None
    error_message: str | None = None
    duration_ms: int | None = None


class StatusSummary(BaseModel):
    last_run_id: int | None = None
    last_run_status: str | None = None
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    sources: list[SourceHealthSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# League / owner / season
# ---------------------------------------------------------------------------


class LeagueSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    league_id: str
    name: str | None = None
    platform: str
    current_season_year: int | None = None
    season_count: int = 0
    owner_count: int = 0
    created_at: datetime
    updated_at: datetime


class OwnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    owner_id: int
    league_id: str
    display_name: str | None = None
    nfl_user_id: str | None = None
    is_active: bool
    joined_year: int | None = None
    left_year: int | None = None


class SeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    season_id: int
    league_id: str
    year: int
    status: str | None = None
    regular_season_weeks: int | None = None
    playoff_weeks: int | None = None
    champion_team_id: int | None = None
    runner_up_team_id: int | None = None
    last_place_team_id: int | None = None


# ---------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------


class ScoringRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_id: int
    season_id: int
    category: str
    stat_key: str
    points_per_unit: float | None = None
    unit_size: float | None = None
    threshold_min: float | None = None
    threshold_max: float | None = None
    flat_points: float | None = None
    raw_text: str | None = None


class ScoringRulesDiffEntry(BaseModel):
    stat_key: str
    category: str
    from_value: ScoringRuleOut | None = None
    to_value: ScoringRuleOut | None = None
    change: str  # "added" | "removed" | "modified"


class ScoringRulesDiff(BaseModel):
    league_id: str
    from_year: int
    to_year: int
    changes: list[ScoringRulesDiffEntry]


# ---------------------------------------------------------------------------
# Team / matchup / transaction
# ---------------------------------------------------------------------------


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    team_id: int
    season_id: int
    owner_id: int
    team_name: str | None = None
    team_abbrev: str | None = None
    draft_position: int | None = None
    final_rank: int | None = None
    regular_season_wins: int | None = None
    regular_season_losses: int | None = None
    regular_season_ties: int | None = None
    regular_season_points_for: float | None = None
    regular_season_points_against: float | None = None
    made_playoffs: bool | None = None
    playoff_finish: int | None = None


class StandingsRow(BaseModel):
    team_id: int
    team_name: str | None = None
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float


class Standings(BaseModel):
    season_id: int
    through_week: int | None = None
    rows: list[StandingsRow]


class PlayerLite(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    name_full: str
    position: str | None = None
    nfl_team: str | None = None


class RosterSlot(BaseModel):
    roster_slot: str | None = None
    is_starter: bool | None = None
    player: PlayerLite
    acquisition_type: str | None = None
    acquisition_week: int | None = None


class TeamRoster(BaseModel):
    team_id: int
    team_name: str | None = None
    season_year: int
    week: int
    slots: list[RosterSlot]


class MatchupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    matchup_id: int
    season_id: int
    week: int
    team_id: int
    opponent_team_id: int | None = None
    team_score: float | None = None
    opponent_score: float | None = None
    is_win: bool | None = None
    is_playoff: bool
    is_consolation: bool


class BoxScoreLineupEntry(BaseModel):
    """One player's line in a matchup box score.

    ``status`` explains why ``league_points`` is what it is, so a client can
    distinguish a real zero from a missing score:

    * ``"played"`` — the player has a scored row; ``league_points`` is the
      real result (which may legitimately be ``0.0`` or negative).
    * ``"bye"`` — no scored row because the player's NFL team had no game
      that week.
    * ``"ir"`` — no scored row; the player sat in a reserve/IR roster slot.
    * ``"did_not_play"`` — no scored row though the player's team played
      (inactive / healthy scratch), or the franchise could not be determined.

    For ``"bye"``/``"ir"``/``"did_not_play"`` the player simply has no stat
    data, so ``league_points`` is ``null`` and ``raw_stats`` is empty.
    """

    roster_slot: str | None = None
    player_id: int
    player_name: str
    raw_stats: dict[str, Any] = Field(default_factory=dict)
    league_points: float | None = None
    breakdown: dict[str, float] = Field(default_factory=dict)
    status: str = "played"


class BoxScoreSide(BaseModel):
    team_id: int
    team_name: str | None = None
    owner_name: str | None = None
    total_score: float | None = None
    lineup: list[BoxScoreLineupEntry]


class BoxScore(BaseModel):
    matchup_id: int
    season_year: int
    week: int
    is_playoff: bool
    home: BoxScoreSide
    away: BoxScoreSide | None = None
    winner_team_id: int | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: int
    season_id: int
    transaction_type: str
    executed_at: datetime | None = None
    effective_week: int | None = None
    team_id: int | None = None
    counterpart_team_id: int | None = None
    player_id: int | None = None
    direction: str | None = None
    waiver_priority_used: int | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Owners (aggregate)
# ---------------------------------------------------------------------------


class OwnerSeasonRecord(BaseModel):
    season_year: int
    team_id: int
    team_name: str | None = None
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    points_for: float | None = None
    final_rank: int | None = None


class OwnerHistory(BaseModel):
    owner_id: int
    display_name: str | None = None
    seasons: list[OwnerSeasonRecord]


class OwnerAggregate(BaseModel):
    owner_id: int
    display_name: str | None = None
    seasons_played: int
    total_wins: int
    total_losses: int
    total_ties: int
    total_points_for: float
    championships: int


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    name_full: str
    name_first: str | None = None
    name_last: str | None = None
    position: str | None = None
    nfl_team: str | None = None
    birth_date: date | None = None
    rookie_year: int | None = None
    # Last NFL season the player appeared in (nflverse fact). NULL when nflverse
    # can't identify the player (e.g. NFL.com-only rows with no gsis_id).
    last_season: int | None = None
    is_active: bool
    # League-relevance span: first/last season the player was rostered in THIS
    # league. NULL ⇒ never rostered here. Lets the client show "rostered
    # 2012-2018" and derive an active/retired-in-league badge without its own
    # joins. Pair with the ``league_relevant`` list filter.
    first_rostered_season: int | None = None
    last_rostered_season: int | None = None
    nfl_com_player_id: str | None = None
    gsis_id: str | None = None
    sleeper_id: str | None = None
    espn_id: str | None = None
    yahoo_id: str | None = None


class RawStatsEntry(BaseModel):
    source: str
    stats: dict[str, Any] = Field(default_factory=dict)


class PlayerStatsBreakdown(BaseModel):
    player_id: int
    season_year: int
    week: int
    raw_stats: dict[str, Any] = Field(default_factory=dict)
    league_points: float | None = None
    points_breakdown: dict[str, Any] = Field(default_factory=dict)
    all_sources: list[RawStatsEntry] = Field(default_factory=list)


class OwnershipEvent(BaseModel):
    team_id: int
    team_name: str | None = None
    season_year: int
    week: int
    roster_slot: str | None = None
    acquisition_type: str | None = None
    acquisition_date: datetime | None = None
    drop_date: datetime | None = None


class ProjectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    projection_id: int
    player_id: int
    season_year: int
    week: int
    source: str
    projected_points: float | None = None
    projected_stats: dict[str, Any] | None = None
    fetched_at: datetime


class AvailabilityRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    availability_id: int
    player_id: int
    season_year: int
    week: int
    status: str
    owning_team_id: int | None = None
    is_pre_kickoff_snapshot: bool
    last_status_change: datetime | None = None


# ---------------------------------------------------------------------------
# Stats aggregates
# ---------------------------------------------------------------------------


class TopScorer(BaseModel):
    player_id: int
    name_full: str
    position: str | None = None
    nfl_team: str | None = None
    season_year: int
    week: int | None = None
    points: float


class SeasonTotal(BaseModel):
    player_id: int
    name_full: str
    position: str | None = None
    nfl_team: str | None = None
    total_points: float
    weeks_played: int


class OwnerCareer(BaseModel):
    owner_id: int
    display_name: str | None = None
    seasons_played: int
    total_wins: int
    total_losses: int
    total_ties: int
    total_points_for: float
    championships: int
