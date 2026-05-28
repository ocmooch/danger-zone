"""SQLAlchemy 2.0 ORM models for the fantasy-football pipeline.

Schema is documented in ``docs/04_DATA_MODEL.md``. Conventions:

* Primary keys are explicit ``{entity}_id`` columns.
* Foreign keys are explicit and named.
* Every table carries ``created_at`` / ``updated_at`` timestamps.
* JSON-shaped columns use SQLAlchemy's ``JSON`` type (TEXT on SQLite,
  JSONB-equivalent on PostgreSQL).
* The schema uses only SQL features available in both SQLite and
  PostgreSQL so the eventual swap is a ``DATABASE_URL`` change.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ff_pipeline.repository.database import Base

# ---------------------------------------------------------------------------
# Reusable column types
# ---------------------------------------------------------------------------


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Core league / season / owner / team
# ---------------------------------------------------------------------------


class League(Base):
    __tablename__ = "leagues"

    league_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String, nullable=False, default="nfl_com")
    current_season_year: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    seasons: Mapped[list[Season]] = relationship(back_populates="league")
    owners: Mapped[list[Owner]] = relationship(back_populates="league")


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("league_id", "year", name="uq_seasons_league_year"),)

    season_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("leagues.league_id", name="fk_seasons_league"),
        nullable=False,
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str | None] = mapped_column(String)  # completed | in_progress | pre_draft
    regular_season_weeks: Mapped[int | None] = mapped_column(Integer)
    playoff_weeks: Mapped[int | None] = mapped_column(Integer)
    champion_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_seasons_champion", use_alter=True),
    )
    runner_up_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_seasons_runner_up", use_alter=True),
    )
    last_place_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_seasons_last_place", use_alter=True),
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    league: Mapped[League] = relationship(back_populates="seasons")
    teams: Mapped[list[Team]] = relationship(back_populates="season", foreign_keys="Team.season_id")
    scoring_rules: Mapped[list[ScoringRule]] = relationship(back_populates="season")
    matchups: Mapped[list[Matchup]] = relationship(back_populates="season")


class Owner(Base):
    __tablename__ = "owners"

    owner_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("leagues.league_id", name="fk_owners_league"),
        nullable=False,
    )
    display_name: Mapped[str | None] = mapped_column(String)
    nfl_user_id: Mapped[str | None] = mapped_column(String)
    aliases: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_year: Mapped[int | None] = mapped_column(Integer)
    left_year: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    league: Mapped[League] = relationship(back_populates="owners")
    teams: Mapped[list[Team]] = relationship(back_populates="owner")


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("season_id", "team_name", name="uq_teams_season_team_name"),
        UniqueConstraint("season_id", "owner_id", name="uq_teams_season_owner"),
    )

    team_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.season_id", name="fk_teams_season"),
        nullable=False,
    )
    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("owners.owner_id", name="fk_teams_owner"),
        nullable=False,
    )
    team_name: Mapped[str | None] = mapped_column(String)
    team_abbrev: Mapped[str | None] = mapped_column(String(8))
    draft_position: Mapped[int | None] = mapped_column(Integer)
    final_rank: Mapped[int | None] = mapped_column(Integer)
    regular_season_wins: Mapped[int | None] = mapped_column(Integer)
    regular_season_losses: Mapped[int | None] = mapped_column(Integer)
    regular_season_ties: Mapped[int | None] = mapped_column(Integer)
    regular_season_points_for: Mapped[float | None] = mapped_column(Float)
    regular_season_points_against: Mapped[float | None] = mapped_column(Float)
    made_playoffs: Mapped[bool | None] = mapped_column(Boolean)
    playoff_finish: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    season: Mapped[Season] = relationship(back_populates="teams", foreign_keys=[season_id])
    owner: Mapped[Owner] = relationship(back_populates="teams")


# ---------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------


class ScoringRule(Base):
    __tablename__ = "scoring_rules"
    __table_args__ = (
        UniqueConstraint(
            "season_id",
            "category",
            "stat_key",
            "threshold_min",
            name="uq_scoring_rules_season_cat_key_thresh",
        ),
    )

    rule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.season_id", name="fk_scoring_rules_season"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    stat_key: Mapped[str] = mapped_column(String, nullable=False)
    points_per_unit: Mapped[float | None] = mapped_column(Float)
    unit_size: Mapped[float | None] = mapped_column(Float, default=1.0)
    threshold_min: Mapped[float | None] = mapped_column(Float)
    threshold_max: Mapped[float | None] = mapped_column(Float)
    flat_points: Mapped[float | None] = mapped_column(Float)
    raw_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    season: Mapped[Season] = relationship(back_populates="scoring_rules")


# ---------------------------------------------------------------------------
# Players + per-week point-in-time snapshots
# ---------------------------------------------------------------------------


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        # gsis_id is the canonical NFL ID used by nflverse, which is the
        # nflverse crawler's natural upsert key. UNIQUE (not just an INDEX)
        # so ON CONFLICT can target it. Nullable so non-nflverse-known
        # players (e.g. seen first on NFL.com) can land before M7 normalizes
        # them.
        UniqueConstraint("gsis_id", name="uq_players_gsis_id"),
        Index("ix_players_sleeper_id", "sleeper_id"),
        Index("ix_players_nfl_com_player_id", "nfl_com_player_id"),
    )

    player_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_full: Mapped[str] = mapped_column(String, nullable=False)
    name_first: Mapped[str | None] = mapped_column(String)
    name_last: Mapped[str | None] = mapped_column(String)
    position: Mapped[str | None] = mapped_column(String(8))
    nfl_team: Mapped[str | None] = mapped_column(String(8))
    birth_date: Mapped[date | None] = mapped_column(Date)
    rookie_year: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    nfl_com_player_id: Mapped[str | None] = mapped_column(String)
    gsis_id: Mapped[str | None] = mapped_column(String)
    sleeper_id: Mapped[str | None] = mapped_column(String)
    espn_id: Mapped[str | None] = mapped_column(String)
    yahoo_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TeamRoster(Base):
    __tablename__ = "team_rosters"
    __table_args__ = (
        UniqueConstraint("team_id", "player_id", "week", name="uq_team_rosters_team_player_week"),
        Index("ix_team_rosters_team_week", "team_id", "week"),
        Index("ix_team_rosters_player_season", "player_id", "season_year"),
        Index("ix_team_rosters_player_acquisition", "player_id", "acquisition_date"),
    )

    roster_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_team_rosters_team"),
        nullable=False,
    )
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_team_rosters_player"),
        nullable=False,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    roster_slot: Mapped[str | None] = mapped_column(String(16))
    is_starter: Mapped[bool | None] = mapped_column(Boolean)
    was_locked_at_kickoff: Mapped[bool | None] = mapped_column(Boolean)
    acquisition_type: Mapped[str | None] = mapped_column(String(32))
    acquisition_week: Mapped[int | None] = mapped_column(Integer)
    acquisition_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    drop_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class PlayerAvailability(Base):
    __tablename__ = "player_availability"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "season_year",
            "week",
            "is_pre_kickoff_snapshot",
            name="uq_player_availability_player_season_week_kickoff",
        ),
        Index("ix_player_availability_season_week_status", "season_year", "week", "status"),
        Index("ix_player_availability_team_week", "owning_team_id", "week"),
    )

    availability_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_player_availability_player"),
        nullable=False,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    owning_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_player_availability_team"),
    )
    waiver_claim_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_change: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_pre_kickoff_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------
# Matchups + transactions
# ---------------------------------------------------------------------------


class Matchup(Base):
    __tablename__ = "matchups"
    __table_args__ = (
        UniqueConstraint("season_id", "week", "team_id", name="uq_matchups_season_week_team"),
    )

    matchup_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.season_id", name="fk_matchups_season"),
        nullable=False,
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    team_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_matchups_team"),
        nullable=False,
    )
    opponent_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_matchups_opponent"),
    )
    team_score: Mapped[float | None] = mapped_column(Float)
    opponent_score: Mapped[float | None] = mapped_column(Float)
    is_win: Mapped[bool | None] = mapped_column(Boolean)
    is_playoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_consolation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nfl_com_game_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    season: Mapped[Season] = relationship(back_populates="matchups")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_season_team", "season_id", "team_id"),
        Index("ix_transactions_season_player", "season_id", "player_id"),
    )

    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.season_id", name="fk_transactions_season"),
        nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_week: Mapped[int | None] = mapped_column(Integer)
    team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_transactions_team"),
    )
    counterpart_team_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("teams.team_id", name="fk_transactions_counterpart"),
    )
    player_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_transactions_player"),
    )
    direction: Mapped[str | None] = mapped_column(String(8))
    waiver_priority_used: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------
# Stats: raw, scored, projections
# ---------------------------------------------------------------------------


class PlayerStatsRaw(Base):
    __tablename__ = "player_stats_raw"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "season_year",
            "week",
            "source",
            name="uq_player_stats_raw_player_season_week_source",
        ),
        Index("ix_player_stats_raw_season_week", "season_year", "week"),
    )

    stat_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_player_stats_raw_player"),
        nullable=False,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    season_type: Mapped[str | None] = mapped_column(String(8))  # REG | POST | PRE
    nfl_opponent: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class PlayerStatsScored(Base):
    __tablename__ = "player_stats_scored"
    __table_args__ = (
        UniqueConstraint("stat_id", "season_id", name="uq_player_stats_scored_stat_season"),
    )

    scored_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stat_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("player_stats_raw.stat_id", name="fk_player_stats_scored_stat"),
        nullable=False,
    )
    season_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("seasons.season_id", name="fk_player_stats_scored_season"),
        nullable=False,
    )
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_player_stats_scored_player"),
        nullable=False,
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    total_points: Mapped[float | None] = mapped_column(Float)
    points_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Projection(Base):
    __tablename__ = "projections"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "season_year",
            "week",
            "source",
            "fetched_at",
            name="uq_projections_player_season_week_source_fetched",
        ),
        Index("ix_projections_season_week", "season_year", "week"),
    )

    projection_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_projections_player"),
        nullable=False,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    projected_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    projected_points: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TrendingPlayer(Base):
    """Snapshot of Sleeper's trending adds/drops for one (player, kind).

    Sleeper exposes ``add`` and ``drop`` trending lists with a sliding
    ``lookback_hours`` window. We store one row per fetch so historical
    trend data is preserved — useful for "why was this guy hot last
    Tuesday?" investigations and for the M9 waiver-priority signal work.
    """

    __tablename__ = "trending_players"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "trend_type",
            "lookback_hours",
            "fetched_at",
            name="uq_trending_players_player_type_lookback_fetched",
        ),
        Index("ix_trending_players_fetched_type", "fetched_at", "trend_type"),
    )

    trending_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.player_id", name="fk_trending_players_player"),
        nullable=False,
    )
    trend_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'add' | 'drop'
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    lookback_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(32))
    sources_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class SourceHealth(Base):
    __tablename__ = "source_health"

    health_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("pipeline_runs.run_id", name="fk_source_health_run"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rows_added: Mapped[int | None] = mapped_column(Integer)
    rows_updated: Mapped[int | None] = mapped_column(Integer)
    parse_failures: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


__all__ = [
    "Base",
    "League",
    "Matchup",
    "Owner",
    "PipelineRun",
    "Player",
    "PlayerAvailability",
    "PlayerStatsRaw",
    "PlayerStatsScored",
    "Projection",
    "ScoringRule",
    "Season",
    "SourceHealth",
    "Team",
    "TeamRoster",
    "Transaction",
    "TrendingPlayer",
]
