"""Source-neutral reads across the NFL.com and Sleeper eras (S8).

Re-exported by ``repository.queries`` (the dashboard's import surface). Read-only.
Plan and decisions: ``docs/plans/S8-read-surface.md``.

* **Points:** one definition of a player-week's league score for both eras.
* **Platform + freshness:** which platform scored a season, and whether its data
  is final or still the season's current, changing state (S8-D3).
* **Capability matrix:** per season, the state of every feature of the master
  plan §4.2: a platform default, overridden by what the data shows.
* **New-feature reads:** brackets, traded picks, status as of a date, failed
  claims, pick/FAAB trade legs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, and_, cast, func, select

from ff_pipeline.repository.models import (
    Matchup,
    PlayerAvailability,
    PlayerInjuryReport,
    PlayerStatsScored,
    PlayerStatusSnapshot,
    PlayoffBracket,
    Season,
    Team,
    TeamRoster,
    TradedPick,
    Transaction,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session
    from sqlalchemy.sql.elements import ColumnElement

PLATFORM_LABELS = {"nfl_com": "NFL.com", "sleeper": "Sleeper"}

# Capability states. ``not_used`` = the league didn't use the feature that season;
# it carries no note and consumers render nothing for it (S8-D1, "resolve silently").
AVAILABLE = "available"
PARTIAL = "partial"
DERIVED = "derived"  # not stored; the dashboard computes it
NOT_YET = "not_yet"  # active season, not produced yet
NOT_ON_PLATFORM = "not_on_platform"
NOT_USED = "not_used"
STATES = (AVAILABLE, PARTIAL, DERIVED, NOT_YET, NOT_ON_PLATFORM, NOT_USED)

CAPABILITY_KEYS = (
    "standings",
    "matchups",
    "rosters_weekly",
    "draft",
    "official_player_points",
    "reconstructed_player_points",
    "transactions_moves",
    "faab_winning_bids",
    "faab_failed_bids",
    "lineup_change_log",
    "setting_change_log",
    "official_brackets",
    "official_divisions",
    "potential_points",
    "trades_with_picks_faab",
    "traded_future_picks",
    "co_owners",
    "commissioner_score_overrides",
    "player_status_daily",
    "availability_weekly",
    "avatars",
)

STATUS_MAX_AGE_DAYS = 7  # S8-D5


# ── points ───────────────────────────────────────────────────────────────────


def official_slot_points_expr(roster: type[TeamRoster] = TeamRoster) -> ColumnElement[float]:
    """The platform's official points for a roster row: Sleeper ``platform_points``,
    else NFL.com ``nfl_com_points`` (NULL when neither)."""
    return func.coalesce(
        cast(func.json_extract(roster.extra_data, "$.platform_points"), Float),
        cast(func.json_extract(roster.extra_data, "$.nfl_com_points"), Float),
    )


def authoritative_week_points_expr(
    roster: type[TeamRoster] = TeamRoster,
    scored: type[PlayerStatsScored] = PlayerStatsScored,
) -> ColumnElement[float]:
    """A player-week's league score: the official points, else the reconstruction.

    Same contract as the dashboard's ``analytics.scoring.authoritative_week_points``
    (LEFT JOIN ``roster`` on ``(player_id, season_year, week)``), for both eras."""
    return func.coalesce(official_slot_points_expr(roster), scored.total_points)


# ── platform + freshness ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    platform: str
    label: str


def season_platform(session: Session, season_id: int) -> PlatformInfo | None:
    season = session.get(Season, season_id)
    if season is None:
        return None
    p = season.platform or "nfl_com"
    return PlatformInfo(platform=p, label=PLATFORM_LABELS.get(p, p))


@dataclass(frozen=True, slots=True)
class SeasonFreshness:
    """``is_final``: the season is over and its data static. Otherwise every
    in-season output is the current state and may still change (S8-D3)."""

    is_final: bool
    last_finalized_week: int | None
    provisional_weeks: tuple[int, ...] = ()


def season_freshness(session: Session, season_id: int) -> SeasonFreshness | None:
    season = session.get(Season, season_id)
    if season is None:
        return None
    final_by_week: dict[int, bool] = {}
    for week, extra in session.execute(
        select(Matchup.week, Matchup.extra_data).where(Matchup.season_id == season_id)
    ).tuples():
        fin = (extra or {}).get("finalized")
        ok = fin is not False  # legacy rows carry no flag: final
        final_by_week[week] = final_by_week.get(week, True) and ok
    finalized = [w for w, ok in final_by_week.items() if ok]
    provisional = tuple(sorted(w for w, ok in final_by_week.items() if not ok))
    return SeasonFreshness(
        # NFL.com is defunct: every NFL.com season is over
        is_final=not provisional
        and (season.status == "completed" or (season.platform or "nfl_com") == "nfl_com"),
        last_finalized_week=max(finalized) if finalized else None,
        provisional_weeks=provisional,
    )


def is_provisional_matchup(m: Matchup) -> bool:
    return (m.extra_data or {}).get("finalized") is False


# ── capability matrix ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Capability:
    state: str
    note: str | None = None


@dataclass(slots=True)
class _Facts:
    platform: str
    year: int
    active: bool
    counts: dict[str, int] = field(default_factory=dict)


def season_capabilities(session: Session, season_id: int) -> dict[str, Capability]:
    """Every master-plan capability for one season (``01`` §8.3, S8-D1/D2).

    Computed from the current data on each call, so an active season's matrix
    follows the season (``not_yet`` exists only while it is active)."""
    season = session.get(Season, season_id)
    if season is None:
        return {}
    fresh = season_freshness(session, season_id)
    f = _Facts(
        platform=season.platform or "nfl_com",
        year=season.year,
        active=not (fresh.is_final if fresh else False),
        counts=_counts(session, season),
    )
    return {key: _capability(key, f) for key in CAPABILITY_KEYS}


def _counts(session: Session, season: Season) -> dict[str, int]:
    sid, yr = season.season_id, season.year
    team_ids = select(Team.team_id).where(Team.season_id == sid).scalar_subquery()

    def n(stmt: Any) -> int:
        return int(session.scalar(stmt) or 0)

    def tx(*conds: Any) -> int:
        return n(select(func.count()).where(Transaction.season_id == sid, *conds))

    weekly = and_(
        TeamRoster.season_year == yr, TeamRoster.week >= 1, TeamRoster.team_id.in_(team_ids)
    )
    official = official_slot_points_expr(TeamRoster)
    is_sleeper = (season.platform or "nfl_com") == "sleeper"
    return {
        "teams": n(select(func.count()).where(Team.season_id == sid)),
        "matchups": n(select(func.count()).where(Matchup.season_id == sid)),
        "decided": n(
            select(func.count()).where(Matchup.season_id == sid, Matchup.is_win.is_not(None))
        ),
        "rosters": n(select(func.count()).where(weekly)),
        "rosters_official": n(select(func.count()).where(weekly, official.is_not(None))),
        "draft": tx(Transaction.transaction_type == "draft"),
        "scored": n(select(func.count()).where(PlayerStatsScored.season_id == sid)),
        "moves": tx(
            Transaction.transaction_type.in_(["free_agent_add", "waiver_add", "drop", "trade"])
        ),
        "faab_bids": tx(
            Transaction.transaction_type == "waiver_add",
            func.json_extract(Transaction.extra_data, "$.faab_bid").is_not(None),
        ),
        "failed": tx(Transaction.status == "failed"),
        "lineup": tx(Transaction.transaction_type == "lineup_change"),
        "setting": tx(Transaction.transaction_type == "setting_change"),
        "trade_assets": tx(
            Transaction.transaction_type == "trade", Transaction.player_id.is_(None)
        ),
        "brackets": n(select(func.count()).where(PlayoffBracket.season_id == sid)),
        "divisions": n(
            select(func.count()).where(Team.season_id == sid, Team.division.is_not(None))
        ),
        "ppts": n(
            select(func.count()).where(
                Team.season_id == sid, func.json_extract(Team.extra_data, "$.ppts").is_not(None)
            )
        ),
        "co_owners": n(
            select(func.count()).where(
                Team.season_id == sid,
                func.json_extract(Team.extra_data, "$.co_owner_user_ids").is_not(None),
            )
        ),
        "overrides": n(
            select(func.count()).where(
                Matchup.season_id == sid,
                func.json_extract(Matchup.extra_data, "$.custom_points").is_not(None),
            )
        ),
        "status_snapshots": n(
            select(func.count()).where(
                PlayerStatusSnapshot.as_of_date >= date(yr, 8, 1),
                PlayerStatusSnapshot.as_of_date < date(yr + 1, 3, 1),
            )
        )
        if is_sleeper
        else 0,
        "injury_reports": n(select(func.count()).where(PlayerInjuryReport.season_year == yr)),
        "availability": n(select(func.count()).where(PlayerAvailability.season_year == yr)),
        "avatars": n(
            select(func.count()).where(
                Team.season_id == sid,
                (Team.team_avatar_asset_id.is_not(None))
                | (Team.owner_avatar_asset_id.is_not(None)),
            )
        ),
    }


def _core(present: int, f: _Facts, *, of: int | None = None, what: str = "") -> Capability:
    """Core data: present → available (partial when only some rows have it);
    absent → not_yet while active, else partial (nothing loaded)."""
    if present and (of is None or present >= of):
        return Capability(AVAILABLE)
    if present:
        return Capability(PARTIAL, f"{present} of {of} {what}".strip())
    return Capability(NOT_YET) if f.active else Capability(PARTIAL, "none loaded")


def _capability(key: str, f: _Facts) -> Capability:
    c, sleeper = f.counts, f.platform == "sleeper"
    if key == "standings":
        return _core(c["decided"], f)
    if key == "matchups":
        return _core(c["matchups"], f)
    if key == "rosters_weekly":
        return _core(c["rosters"], f)
    if key == "draft":
        return _core(c["draft"], f)
    if key == "official_player_points":
        return _core(c["rosters_official"], f, of=c["rosters"], what="roster rows")
    if key == "reconstructed_player_points":
        return _core(c["scored"], f)
    if key == "transactions_moves":
        return _core(c["moves"], f)
    if key == "faab_winning_bids":
        return Capability(AVAILABLE) if c["faab_bids"] else Capability(NOT_USED)
    if key == "faab_failed_bids":  # activity: available even with none (S8-D2)
        return Capability(AVAILABLE) if sleeper or c["failed"] else Capability(NOT_ON_PLATFORM)
    if key in ("lineup_change_log", "setting_change_log"):
        rows = c["lineup" if key == "lineup_change_log" else "setting"]
        if rows:
            return Capability(AVAILABLE)
        if sleeper:
            return Capability(
                NOT_ON_PLATFORM,
                "Sleeper exposes no history; settings snapshot instead"
                if key == "setting_change_log"
                else "Sleeper exposes no history",
            )
        return Capability(PARTIAL, "none loaded")
    if key == "official_brackets":
        if c["brackets"]:
            return Capability(AVAILABLE)
        return Capability(NOT_YET) if sleeper and f.active else Capability(DERIVED)
    if key == "official_divisions":  # structure: silent when unused (S8-D2)
        if c["divisions"]:
            return Capability(AVAILABLE)
        if not sleeper and f.year <= 2019:
            return Capability(DERIVED, "divisions derived by the dashboard (2010-2019)")
        return Capability(NOT_USED)
    if key == "potential_points":
        return Capability(AVAILABLE) if c["ppts"] else Capability(DERIVED)
    if key in ("trades_with_picks_faab", "traded_future_picks"):  # activity
        return (
            Capability(AVAILABLE) if sleeper or c["trade_assets"] else Capability(NOT_ON_PLATFORM)
        )
    if key == "co_owners":  # structure
        if c["co_owners"]:
            return Capability(AVAILABLE)
        return Capability(NOT_USED) if sleeper else Capability(NOT_ON_PLATFORM)
    if key == "commissioner_score_overrides":  # structure
        if c["overrides"]:
            return Capability(AVAILABLE)
        return Capability(NOT_USED) if sleeper else Capability(NOT_ON_PLATFORM)
    if key == "player_status_daily":
        if c["status_snapshots"]:
            return Capability(AVAILABLE)
        if c["injury_reports"]:
            return Capability(PARTIAL, "weekly nflverse injury reports")
        return Capability(NOT_YET) if f.active else Capability(NOT_ON_PLATFORM)
    if key == "availability_weekly":
        if c["availability"]:
            return Capability(PARTIAL, "NFL.com availability snapshots")
        return Capability(DERIVED) if sleeper else Capability(NOT_ON_PLATFORM)
    if key == "avatars":
        return _core(c["avatars"], f, of=c["teams"], what="teams")
    raise KeyError(key)


# ── new-feature reads ────────────────────────────────────────────────────────


def brackets_for_season(session: Session, season_id: int) -> list[PlayoffBracket]:
    return list(
        session.scalars(
            select(PlayoffBracket)
            .where(PlayoffBracket.season_id == season_id)
            .order_by(PlayoffBracket.bracket.desc(), PlayoffBracket.round, PlayoffBracket.match_no)
        )
    )


def traded_picks_for_league(
    session: Session, league_id: str, *, pick_season: int | None = None
) -> list[TradedPick]:
    stmt = select(TradedPick).where(TradedPick.league_id == league_id)
    if pick_season is not None:
        stmt = stmt.where(TradedPick.pick_season == pick_season)
    return list(
        session.scalars(
            stmt.order_by(TradedPick.pick_season, TradedPick.round, TradedPick.original_owner_id)
        )
    )


def player_status_asof(
    session: Session,
    player_ids: Iterable[int],
    as_of: date,
    *,
    max_age_days: int = STATUS_MAX_AGE_DAYS,
    source: str = "sleeper",
) -> dict[int, PlayerStatusSnapshot]:
    """The newest snapshot per player on or before ``as_of``, if not older than
    ``max_age_days`` (S8-D5); players without one are absent (an honest gap)."""
    ids = list(player_ids)
    if not ids:
        return {}
    rows = session.scalars(
        select(PlayerStatusSnapshot)
        .where(
            PlayerStatusSnapshot.player_id.in_(ids),
            PlayerStatusSnapshot.source == source,
            PlayerStatusSnapshot.as_of_date <= as_of,
            PlayerStatusSnapshot.as_of_date >= as_of - timedelta(days=max_age_days),
        )
        .order_by(PlayerStatusSnapshot.as_of_date)
    )
    return {r.player_id: r for r in rows}  # ordered ascending: the newest wins


def failed_waiver_claims(
    session: Session, season_id: int, *, team_id: int | None = None, week: int | None = None
) -> list[Transaction]:
    stmt = select(Transaction).where(
        Transaction.season_id == season_id, Transaction.status == "failed"
    )
    if team_id is not None:
        stmt = stmt.where(Transaction.team_id == team_id)
    if week is not None:
        stmt = stmt.where(Transaction.effective_week == week)
    return list(session.scalars(stmt.order_by(Transaction.executed_at, Transaction.transaction_id)))


def trade_assets(session: Session, season_id: int) -> list[Transaction]:
    """The player-less ``trade`` legs carrying picks / FAAB (S7-D5)."""
    return list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.season_id == season_id,
                Transaction.transaction_type == "trade",
                Transaction.player_id.is_(None),
            )
            .order_by(Transaction.executed_at, Transaction.transaction_id)
        )
    )


__all__ = [
    "AVAILABLE",
    "CAPABILITY_KEYS",
    "DERIVED",
    "NOT_ON_PLATFORM",
    "NOT_USED",
    "NOT_YET",
    "PARTIAL",
    "PLATFORM_LABELS",
    "STATES",
    "STATUS_MAX_AGE_DAYS",
    "Capability",
    "PlatformInfo",
    "SeasonFreshness",
    "authoritative_week_points_expr",
    "brackets_for_season",
    "failed_waiver_claims",
    "is_provisional_matchup",
    "official_slot_points_expr",
    "player_status_asof",
    "season_capabilities",
    "season_freshness",
    "season_platform",
    "trade_assets",
    "traded_picks_for_league",
]
