"""Platform-neutral DTOs between a normalizer and the canonical loader (S5).

A normalizer turns platform payloads into these; ``canonical.loader`` is the
only code that writes them. Teams are referenced by their ``platform_team_key``
(not a DB id), players by internal ``player_id`` (already resolved).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class SeasonDTO:
    year: int
    status: str  # pre_draft | in_progress | completed
    regular_season_weeks: int | None
    playoff_weeks: int | None
    platform_league_id: str
    platform_draft_id: str | None
    last_finalized_week: int  # weeks <= this are final (Sleeper's last_scored_leg)
    settings_snapshot: dict[str, Any] | None = None  # S7


@dataclass(frozen=True, slots=True)
class TeamDTO:
    team_key: str  # 'sleeper:roster:<roster_id>'
    owner_id: int
    team_name: str
    team_abbrev: str | None
    draft_position: int | None
    # Sleeper's own record (rosters[].settings), for the cross-check only.
    platform_record: tuple[int, int, int, float, float] | None = None
    # S7: current-state extras, postseason results, avatar source URLs
    division: str | None = None
    extra_data: dict[str, Any] | None = None
    final_rank: int | None = None
    playoff_finish: int | None = None
    made_playoffs: bool | None = None
    team_avatar_url: str | None = None
    owner_avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class MatchupDTO:
    week: int
    team_key: str
    opponent_key: str | None
    team_score: float | None
    opponent_score: float | None
    finalized: bool
    platform_matchup_key: str | None
    extra_data: dict[str, Any] = field(default_factory=dict)
    is_playoff: bool = False  # S7: a bracket game (either bracket)
    is_consolation: bool = False  # S7: a losers-bracket game


@dataclass(frozen=True, slots=True)
class RosterRowDTO:
    week: int  # 0 = draft snapshot
    team_key: str
    player_id: int
    roster_slot: str
    is_starter: bool
    was_locked_at_kickoff: bool
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TxLegDTO:
    platform_transaction_id: str
    transaction_type: str  # free_agent_add | waiver_add | drop | trade | draft
    direction: str  # in | out | add
    team_key: str
    player_id: int | None
    executed_at: datetime | None
    effective_week: int | None
    status: str  # complete | failed
    counterpart_key: str | None = None
    waiver_priority_used: int | None = None
    notes: str | None = None
    extra_data: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, int, str, str]:
        """Idempotency key (mirrors the S3 partial unique index)."""
        return (
            self.platform_transaction_id,
            self.player_id if self.player_id is not None else -1,
            self.team_key,
            self.direction,
        )


@dataclass(frozen=True, slots=True)
class BracketMatchDTO:
    """One playoff bracket match (S7)."""

    bracket: str  # winners | losers
    round: int
    match_no: int
    team1_key: str | None
    team2_key: str | None
    winner_key: str | None
    loser_key: str | None
    placement: int | None  # the place this match decides within its bracket (p)
    team1_from: dict[str, Any] | None = None
    team2_from: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TradedPickDTO:
    """Current owner of one traded future draft pick (S7)."""

    pick_season: int
    round: int
    original_key: str
    current_key: str
    previous_key: str | None
    source_transaction_id: str | None


@dataclass(frozen=True, slots=True)
class SeasonResultsDTO:
    champion_key: str | None = None
    runner_up_key: str | None = None
    last_place_key: str | None = None


@dataclass(slots=True)
class SeasonBundle:
    """Everything one load writes for one season."""

    season: SeasonDTO
    teams: list[TeamDTO] = field(default_factory=list)
    matchups: list[MatchupDTO] = field(default_factory=list)
    rosters: list[RosterRowDTO] = field(default_factory=list)
    transactions: list[TxLegDTO] = field(default_factory=list)
    brackets: list[BracketMatchDTO] = field(default_factory=list)
    traded_picks: list[TradedPickDTO] = field(default_factory=list)
    results: SeasonResultsDTO = field(default_factory=SeasonResultsDTO)
    warnings: list[str] = field(default_factory=list)


__all__ = [
    "BracketMatchDTO",
    "MatchupDTO",
    "RosterRowDTO",
    "SeasonBundle",
    "SeasonDTO",
    "SeasonResultsDTO",
    "TeamDTO",
    "TradedPickDTO",
    "TxLegDTO",
]
