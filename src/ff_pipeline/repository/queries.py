"""Query helpers consumed by the FastAPI read service.

The repository is the only layer in the project allowed to talk to the
database. Routes call into these functions and never touch SQLAlchemy
directly. Each helper either returns an ORM row (or list of rows) or
``None`` when the entity doesn't exist — callers translate ``None`` into
a 404 via ``ApiError``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from ff_pipeline.crawlers.nflverse.franchises import historical_team_code
from ff_pipeline.nfl_teams import canonical_franchise
from ff_pipeline.repository.models import (
    Commissioner,
    League,
    Matchup,
    Owner,
    PipelineRun,
    Player,
    PlayerAvailability,
    PlayerIdentityLink,
    PlayerInjuryReport,
    PlayerSeasonPosition,
    PlayerStatsRaw,
    PlayerStatsScored,
    Projection,
    ScoringRule,
    Season,
    SourceHealth,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.player_identity_integrity import source_identity_mismatches
from ff_pipeline.repository.player_position_integrity import season_position_divergences

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Pipeline meta
# ---------------------------------------------------------------------------


def latest_pipeline_run(session: Session) -> PipelineRun | None:
    """Most recent pipeline run regardless of status — used for ``meta``."""
    stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1)
    return session.execute(stmt).scalars().first()


def source_health_for_run(session: Session, run_id: int) -> list[SourceHealth]:
    stmt = select(SourceHealth).where(SourceHealth.run_id == run_id)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# League
# ---------------------------------------------------------------------------


def list_leagues(session: Session) -> list[League]:
    return list(session.execute(select(League).order_by(League.league_id)).scalars().all())


def get_league(session: Session, league_id: str) -> League | None:
    return session.get(League, league_id)


def count_seasons_for_league(session: Session, league_id: str) -> int:
    stmt = select(func.count(Season.season_id)).where(Season.league_id == league_id)
    return int(session.execute(stmt).scalar_one())


def count_owners_for_league(session: Session, league_id: str) -> int:
    stmt = select(func.count(Owner.owner_id)).where(Owner.league_id == league_id)
    return int(session.execute(stmt).scalar_one())


def list_owners_for_league(session: Session, league_id: str) -> list[Owner]:
    stmt = select(Owner).where(Owner.league_id == league_id).order_by(Owner.owner_id)
    return list(session.execute(stmt).scalars().all())


def list_seasons_for_league(session: Session, league_id: str) -> list[Season]:
    stmt = select(Season).where(Season.league_id == league_id).order_by(Season.year)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Season / scoring rules
# ---------------------------------------------------------------------------


def get_season(session: Session, season_id: int) -> Season | None:
    return session.get(Season, season_id)


def get_season_by_year(session: Session, league_id: str, year: int) -> Season | None:
    stmt = select(Season).where(Season.league_id == league_id, Season.year == year)
    return session.execute(stmt).scalars().first()


def list_scoring_rules(session: Session, season_id: int) -> list[ScoringRule]:
    stmt = (
        select(ScoringRule)
        .where(ScoringRule.season_id == season_id)
        .order_by(ScoringRule.category, ScoringRule.stat_key)
    )
    return list(session.execute(stmt).scalars().all())


def list_teams_for_season(session: Session, season_id: int) -> list[Team]:
    stmt = select(Team).where(Team.season_id == season_id).order_by(Team.team_id)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


def get_team(session: Session, team_id: int) -> Team | None:
    return session.get(Team, team_id)


def get_owner(session: Session, owner_id: int) -> Owner | None:
    return session.get(Owner, owner_id)


def list_teams_for_owner(session: Session, owner_id: int) -> list[Team]:
    stmt = select(Team).where(Team.owner_id == owner_id).order_by(Team.team_id)
    return list(session.execute(stmt).scalars().all())


def roster_for_team_week(
    session: Session, team_id: int, week: int | None
) -> list[tuple[TeamRoster, Player]]:
    """Return ``(roster_row, player)`` pairs for the given team / week.

    When ``week`` is ``None`` the *latest* week present for the team is
    used so ``?week=`` may be omitted on the route.
    """
    target_week = week
    if target_week is None:
        latest = session.execute(
            select(func.max(TeamRoster.week)).where(TeamRoster.team_id == team_id)
        ).scalar_one_or_none()
        if latest is None:
            return []
        target_week = int(latest)
    stmt = (
        select(TeamRoster, Player)
        .join(Player, Player.player_id == TeamRoster.player_id)
        .where(TeamRoster.team_id == team_id, TeamRoster.week == target_week)
        .order_by(TeamRoster.roster_slot.is_(None), TeamRoster.roster_slot)
    )
    return list(session.execute(stmt).all())  # type: ignore[arg-type]


def nfl_franchises_that_played(session: Session, season_year: int, week: int) -> set[str]:
    """Return the canonical franchise codes that had an NFL game that week.

    Derived from the distinct ``nfl_opponent`` values recorded in
    ``player_stats_raw`` for ``(season_year, week)``: every team that took the
    field is some player's opponent, so the set of opponents *is* the set of
    teams that played. Codes are folded via :func:`canonical_franchise` so the
    result compares cleanly against a player's ``nfl_team`` regardless of
    spelling or relocation drift.

    A franchise absent from this set had no game that week — i.e. a bye. The
    set is empty when no stats are ingested for the week yet; callers must
    treat an empty result as "unknown" and not infer byes from it (otherwise a
    not-yet-ingested week would flag every player as on a bye).
    """
    rows = session.execute(
        select(PlayerStatsRaw.nfl_opponent)
        .where(
            PlayerStatsRaw.season_year == season_year,
            PlayerStatsRaw.week == week,
            PlayerStatsRaw.nfl_opponent.isnot(None),
        )
        .distinct()
    ).scalars()
    played: set[str] = set()
    for opponent in rows:
        code = canonical_franchise(opponent)
        if code is not None:
            played.add(code)
    return played


def matchups_for_team(session: Session, team_id: int) -> list[Matchup]:
    stmt = (
        select(Matchup).where(Matchup.team_id == team_id).order_by(Matchup.season_id, Matchup.week)
    )
    return list(session.execute(stmt).scalars().all())


def transactions_for_team(session: Session, team_id: int) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .where(or_(Transaction.team_id == team_id, Transaction.counterpart_team_id == team_id))
        .order_by(Transaction.executed_at)
    )
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Standings (computed from matchups)
# ---------------------------------------------------------------------------


def standings_for_season(
    session: Session, season_id: int, through_week: int | None = None
) -> list[dict[str, Any]]:
    """Aggregate matchups into a {team_id: {wins, losses, ties, pf, pa}} table."""
    conditions = [Matchup.season_id == season_id]
    if through_week is not None:
        conditions.append(Matchup.week <= through_week)
    stmt = select(Matchup).where(*conditions)
    matchups = list(session.execute(stmt).scalars().all())

    teams_in_season = {team.team_id: team for team in list_teams_for_season(session, season_id)}

    by_team: dict[int, dict[str, float]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "ties": 0, "points_for": 0.0, "points_against": 0.0}
    )
    for m in matchups:
        row = by_team[m.team_id]
        if m.is_win is True:
            row["wins"] += 1
        elif m.is_win is False:
            row["losses"] += 1
        else:
            # NULL is_win + both scores set → tie
            if m.team_score is not None and m.opponent_score is not None:
                row["ties"] += 1
        row["points_for"] += m.team_score or 0.0
        row["points_against"] += m.opponent_score or 0.0

    out: list[dict[str, Any]] = []
    for team_id, row in by_team.items():
        team = teams_in_season.get(team_id)
        out.append(
            {
                "team_id": team_id,
                "team_name": team.team_name if team else None,
                "wins": int(row["wins"]),
                "losses": int(row["losses"]),
                "ties": int(row["ties"]),
                "points_for": round(row["points_for"], 2),
                "points_against": round(row["points_against"], 2),
            }
        )
    out.sort(key=lambda r: (-r["wins"], r["losses"], -r["points_for"]))
    return out


def get_matchup(session: Session, matchup_id: int) -> Matchup | None:
    return session.get(Matchup, matchup_id)


def list_matchups(session: Session, *, season_year: int | None, week: int | None) -> list[Matchup]:
    stmt = select(Matchup)
    if season_year is not None:
        stmt = stmt.join(Season, Season.season_id == Matchup.season_id).where(
            Season.year == season_year
        )
    if week is not None:
        stmt = stmt.where(Matchup.week == week)
    stmt = stmt.order_by(Matchup.season_id, Matchup.week, Matchup.matchup_id)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def list_transactions(
    session: Session,
    *,
    season_year: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    stmt = select(Transaction)
    if season_year is not None:
        stmt = stmt.join(Season, Season.season_id == Transaction.season_id).where(
            Season.year == season_year
        )
    if team_id is not None:
        stmt = stmt.where(
            or_(Transaction.team_id == team_id, Transaction.counterpart_team_id == team_id)
        )
    if player_id is not None:
        stmt = stmt.where(Transaction.player_id == player_id)
    stmt = stmt.order_by(Transaction.executed_at).offset(offset).limit(limit)
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def get_player(session: Session, player_id: int) -> Player | None:
    return session.get(Player, player_id)


def search_players(
    session: Session,
    *,
    name: str | None = None,
    position: str | None = None,
    nfl_team: str | None = None,
    active: bool | None = None,
    league_relevant: bool | None = None,
    gsis_id: str | None = None,
    sleeper_id: str | None = None,
    nfl_com_player_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Player]:
    stmt = select(Player)
    if name is not None:
        like = f"%{name}%"
        stmt = stmt.where(Player.name_full.ilike(like))
    if position is not None:
        stmt = stmt.where(Player.position == position)
    if nfl_team is not None:
        stmt = stmt.where(Player.nfl_team == nfl_team)
    if active is not None:
        stmt = stmt.where(Player.is_active.is_(active))
    # League-relevance is a *historical* fact — "was this player ever rostered
    # in THIS league?" — and is distinct from ``active`` (a current-NFL fact).
    # A non-NULL rostered span is the marker; nflverse ships the whole NFL
    # universe and most of it (the "ghost" players) never touched this league.
    if league_relevant is not None:
        if league_relevant:
            stmt = stmt.where(Player.last_rostered_season.is_not(None))
        else:
            stmt = stmt.where(Player.last_rostered_season.is_(None))
    # External-ID filters are exact-match join keys, not fuzzy text — they
    # let Phase 2/3 resolve a player by any platform's ID (the M7 goal:
    # queryable by name, GSIS, Sleeper, or NFL.com ID).
    if gsis_id is not None:
        stmt = stmt.where(Player.gsis_id == gsis_id)
    if sleeper_id is not None:
        stmt = stmt.where(Player.sleeper_id == sleeper_id)
    if nfl_com_player_id is not None:
        stmt = stmt.where(Player.nfl_com_player_id == nfl_com_player_id)
    stmt = stmt.order_by(Player.name_full).offset(offset).limit(limit)
    return list(session.execute(stmt).scalars().all())


def player_raw_stats(
    session: Session, player_id: int, season_year: int, week: int
) -> list[PlayerStatsRaw]:
    stmt = select(PlayerStatsRaw).where(
        PlayerStatsRaw.player_id == player_id,
        PlayerStatsRaw.season_year == season_year,
        PlayerStatsRaw.week == week,
    )
    return list(session.execute(stmt).scalars().all())


def player_scored_stats(
    session: Session, player_id: int, season_year: int, week: int
) -> PlayerStatsScored | None:
    stmt = (
        select(PlayerStatsScored)
        .join(Season, Season.season_id == PlayerStatsScored.season_id)
        .where(
            PlayerStatsScored.player_id == player_id,
            Season.year == season_year,
            PlayerStatsScored.week == week,
        )
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def player_identity_cluster(session: Session, player_id: int) -> dict[str, object] | None:
    """Return canonical identity and member ids for one player.

    When no crosswalk row exists, the player is its own canonical cluster. This
    lets downstream read services consume the helper before the curated link
    table is populated, while still getting the full member set as soon as links
    land.
    """
    if session.get(Player, player_id) is None:
        return None

    link = session.execute(
        select(PlayerIdentityLink).where(PlayerIdentityLink.member_player_id == player_id).limit(1)
    ).scalar_one_or_none()
    canonical_id = int(link.canonical_player_id) if link is not None else player_id
    member_ids = {
        int(pid)
        for pid in session.execute(
            select(PlayerIdentityLink.member_player_id).where(
                PlayerIdentityLink.canonical_player_id == canonical_id
            )
        ).scalars()
    }
    member_ids.add(canonical_id)
    return {
        "player_id": player_id,
        "canonical_player_id": canonical_id,
        "member_player_ids": sorted(member_ids),
    }


def player_source_identity_mismatches(session: Session) -> list[dict[str, Any]]:
    """Strong NFL.com external-ID ownership conflicts for downstream observability."""
    return source_identity_mismatches(session)


def player_season_position_divergences(session: Session) -> list[dict[str, Any]]:
    """Players whose static position disagrees with a rostered season's position."""
    return season_position_divergences(session)


def player_season_teams(
    session: Session, player_ids: Sequence[int], season_year: int
) -> dict[int, str]:
    """Map each player to their season-correct NFL team for ``season_year``.

    Read from the stored per-week ``player_stats_raw.nfl_team`` (the team a
    player was actually on that season). The team of record is the one the
    player appears with in the most primary stat weeks, ties broken by the
    latest week, so a mid-season trade resolves to where they spent most of the
    season. Players with no stored per-week team that season are absent from
    the map — callers fall back to the current snapshot on ``players.nfl_team``.

    The stored value is nflverse's current franchise code (nflverse normalizes
    all of history to it), so it's run through :func:`historical_team_code` to
    render the code in use that season — a 2015 Raider reads "OAK", not "LV".

    Batched so the stats leaderboard can resolve a whole page of players in one
    query instead of N+1.
    """
    if not player_ids:
        return {}
    stmt = (
        select(
            PlayerStatsRaw.player_id,
            PlayerStatsRaw.nfl_team,
            func.count().label("weeks"),
            func.max(PlayerStatsRaw.week).label("last_week"),
        )
        .where(
            PlayerStatsRaw.player_id.in_(player_ids),
            PlayerStatsRaw.season_year == season_year,
            PlayerStatsRaw.nfl_team.isnot(None),
            PlayerStatsRaw.is_primary.is_(True),
        )
        .group_by(PlayerStatsRaw.player_id, PlayerStatsRaw.nfl_team)
    )
    # (weeks, last_week) descending wins per player.
    best: dict[int, tuple[int, int]] = {}
    result: dict[int, str] = {}
    for row in session.execute(stmt).all():
        rank = (int(row.weeks), int(row.last_week))
        if row.player_id not in best or rank > best[row.player_id]:
            best[row.player_id] = rank
            result[row.player_id] = historical_team_code(row.nfl_team, season_year)
    return result


def player_nfl_team(session: Session, player_id: int, season_year: int) -> str | None:
    """Season-correct NFL team for one player-season.

    Thin single-player wrapper over :func:`player_season_teams`; returns
    ``None`` when no per-week team is stored for that player-season.
    """
    return player_season_teams(session, [player_id], season_year).get(player_id)


def player_season_positions(
    session: Session, player_ids: Sequence[int], season_year: int
) -> dict[int, str]:
    """Map each player to their season-correct NFL position for ``season_year``.

    Read from the stored ``player_season_positions`` table (one row per
    player-season, sourced from nflverse's per-season rosters). This is the
    season-aware counterpart to the single current/last-known snapshot on
    ``players.position`` — a player who lined up at WR in 2014 reads "WR" even if
    his stored snapshot later became "TE". Players with no stored row that season
    are absent from the map; callers fall back to ``players.position``.

    Batched so a whole box score / leaderboard page resolves in one query.
    """
    if not player_ids:
        return {}
    stmt = select(
        PlayerSeasonPosition.player_id,
        PlayerSeasonPosition.position,
    ).where(
        PlayerSeasonPosition.player_id.in_(player_ids),
        PlayerSeasonPosition.season_year == season_year,
    )
    return {row.player_id: row.position for row in session.execute(stmt).all()}


def player_position(session: Session, player_id: int, season_year: int) -> str | None:
    """Season-correct NFL position for one player-season.

    Thin single-player wrapper over :func:`player_season_positions`; returns
    ``None`` when no per-season position is stored for that player-season.
    """
    return player_season_positions(session, [player_id], season_year).get(player_id)


def player_ownership(session: Session, player_id: int) -> list[tuple[TeamRoster, Team]]:
    stmt = (
        select(TeamRoster, Team)
        .join(Team, Team.team_id == TeamRoster.team_id)
        .where(TeamRoster.player_id == player_id)
        .order_by(TeamRoster.season_year, TeamRoster.week)
    )
    return list(session.execute(stmt).all())  # type: ignore[arg-type]


def player_projections(
    session: Session, player_id: int, season_year: int | None, week: int | None
) -> list[Projection]:
    stmt = select(Projection).where(Projection.player_id == player_id)
    if season_year is not None:
        stmt = stmt.where(Projection.season_year == season_year)
    if week is not None:
        stmt = stmt.where(Projection.week == week)
    stmt = stmt.order_by(Projection.fetched_at.desc())
    return list(session.execute(stmt).scalars().all())


def player_availability_for_season(
    session: Session, player_id: int, season_year: int
) -> list[PlayerAvailability]:
    stmt = (
        select(PlayerAvailability)
        .where(
            PlayerAvailability.player_id == player_id,
            PlayerAvailability.season_year == season_year,
        )
        .order_by(PlayerAvailability.week, PlayerAvailability.is_pre_kickoff_snapshot.desc())
    )
    return list(session.execute(stmt).scalars().all())


def availability_snapshot(
    session: Session,
    *,
    season_year: int,
    week: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PlayerAvailability]:
    stmt = select(PlayerAvailability).where(
        PlayerAvailability.season_year == season_year,
        PlayerAvailability.week == week,
    )
    if status is not None:
        stmt = stmt.where(PlayerAvailability.status == status)
    stmt = stmt.order_by(PlayerAvailability.player_id).offset(offset).limit(limit)
    return list(session.execute(stmt).scalars().all())


def availability_timeline(session: Session, player_id: int) -> list[PlayerAvailability]:
    stmt = (
        select(PlayerAvailability)
        .where(PlayerAvailability.player_id == player_id)
        .order_by(PlayerAvailability.season_year, PlayerAvailability.week)
    )
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Stats aggregates
# ---------------------------------------------------------------------------


def top_scorers(
    session: Session,
    *,
    season_year: int,
    week: int | None,
    position: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Player.player_id,
            Player.name_full,
            Player.position,
            Player.nfl_team,
            PlayerStatsScored.week,
            PlayerStatsScored.total_points,
        )
        .join(PlayerStatsScored, PlayerStatsScored.player_id == Player.player_id)
        .join(Season, Season.season_id == PlayerStatsScored.season_id)
        .where(Season.year == season_year)
    )
    if week is not None:
        stmt = stmt.where(PlayerStatsScored.week == week)
    if position is not None:
        stmt = stmt.where(Player.position == position)
    stmt = stmt.order_by(PlayerStatsScored.total_points.desc()).limit(limit)
    rows = session.execute(stmt).all()
    # Each row is a single (player, week) score, so render the team the player
    # was on that exact week; fall back to the current snapshot when no
    # per-week team is stored.
    week_teams = _player_week_teams(session, [(r.player_id, r.week) for r in rows], season_year)
    return [
        {
            "player_id": r.player_id,
            "name_full": r.name_full,
            "position": r.position,
            "nfl_team": week_teams.get((r.player_id, r.week)) or r.nfl_team,
            "season_year": season_year,
            "week": r.week,
            "points": float(r.total_points or 0.0),
        }
        for r in rows
    ]


def _player_week_teams(
    session: Session, keys: Sequence[tuple[int, int]], season_year: int
) -> dict[tuple[int, int], str]:
    """Map ``(player_id, week)`` to the player's NFL team that exact week.

    Reads the primary per-week ``player_stats_raw.nfl_team`` for the players in
    ``keys``; pairs with no stored team are absent (caller falls back). The
    stored current code is rendered as the season-era one via
    :func:`historical_team_code` (a 2015 Raider reads "OAK", not "LV").
    """
    if not keys:
        return {}
    player_ids = {pid for pid, _ in keys}
    stmt = select(
        PlayerStatsRaw.player_id,
        PlayerStatsRaw.week,
        PlayerStatsRaw.nfl_team,
    ).where(
        PlayerStatsRaw.player_id.in_(player_ids),
        PlayerStatsRaw.season_year == season_year,
        PlayerStatsRaw.nfl_team.isnot(None),
        PlayerStatsRaw.is_primary.is_(True),
    )
    return {
        (row.player_id, row.week): historical_team_code(row.nfl_team, season_year)
        for row in session.execute(stmt).all()
    }


def season_totals(session: Session, season_year: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            Player.player_id,
            Player.name_full,
            Player.position,
            Player.nfl_team,
            func.sum(PlayerStatsScored.total_points).label("total"),
            func.count(PlayerStatsScored.scored_id).label("weeks"),
        )
        .join(PlayerStatsScored, PlayerStatsScored.player_id == Player.player_id)
        .join(Season, Season.season_id == PlayerStatsScored.season_id)
        .where(Season.year == season_year)
        .group_by(Player.player_id, Player.name_full, Player.position, Player.nfl_team)
        .order_by(func.sum(PlayerStatsScored.total_points).desc())
    )
    rows = session.execute(stmt).all()
    # Render the team the player was on *that* season, not the current
    # snapshot; fall back to the snapshot when no per-week team is stored.
    season_teams = player_season_teams(session, [r.player_id for r in rows], season_year)
    return [
        {
            "player_id": r.player_id,
            "name_full": r.name_full,
            "position": r.position,
            "nfl_team": season_teams.get(r.player_id) or r.nfl_team,
            "total_points": float(r.total or 0.0),
            "weeks_played": int(r.weeks or 0),
        }
        for r in rows
    ]


def owner_career_aggregates(session: Session) -> list[dict[str, Any]]:
    """Aggregate owner career totals by walking teams + championships.

    Wins/losses/ties/points come from each owner's ``teams`` rows.
    Championships count seasons whose ``champion_team_id`` belongs to
    one of this owner's teams.
    """
    owners = list(session.execute(select(Owner)).scalars().all())
    teams = list(session.execute(select(Team)).scalars().all())
    seasons = list(session.execute(select(Season)).scalars().all())

    teams_by_owner: dict[int, list[Team]] = defaultdict(list)
    for team in teams:
        teams_by_owner[team.owner_id].append(team)

    champion_team_ids = {s.champion_team_id for s in seasons if s.champion_team_id is not None}

    rows: list[dict[str, Any]] = []
    for owner in owners:
        owner_teams = teams_by_owner.get(owner.owner_id, [])
        wins = sum(t.regular_season_wins or 0 for t in owner_teams)
        losses = sum(t.regular_season_losses or 0 for t in owner_teams)
        ties = sum(t.regular_season_ties or 0 for t in owner_teams)
        points = sum(t.regular_season_points_for or 0.0 for t in owner_teams)
        champs = sum(1 for t in owner_teams if t.team_id in champion_team_ids)
        rows.append(
            {
                "owner_id": owner.owner_id,
                "display_name": owner.display_name,
                "seasons_played": len(owner_teams),
                "total_wins": int(wins),
                "total_losses": int(losses),
                "total_ties": int(ties),
                "total_points_for": round(points, 2),
                "championships": champs,
            }
        )
    rows.sort(key=lambda r: (-r["championships"], -r["total_wins"]))
    return rows


# ---------------------------------------------------------------------------
# Injury reports
# ---------------------------------------------------------------------------


def injury_reports_for_week(
    session: Session, season_year: int, week: int
) -> dict[int, PlayerInjuryReport]:
    """Return ``player_id → PlayerInjuryReport`` for a given season/week.

    Used by the BFF box score to surface injury status alongside player
    scores. Returns only rows that exist; missing players are absent from
    the dict (caller treats absence as no report).
    """
    rows = (
        session.execute(
            select(PlayerInjuryReport).where(
                PlayerInjuryReport.season_year == season_year,
                PlayerInjuryReport.week == week,
            )
        )
        .scalars()
        .all()
    )
    return {r.player_id: r for r in rows}


# ---------------------------------------------------------------------------
# Commissioners
# ---------------------------------------------------------------------------


def commissioner_terms(session: Session, league_id: str) -> list[Commissioner]:
    """Return a league's commissioner terms ordered oldest-first.

    Manually-curated metadata seeded into the ``commissioners`` table. Each row
    carries its ``owner`` relationship so callers can resolve display names.
    """
    return list(
        session.execute(
            select(Commissioner)
            .where(Commissioner.league_id == league_id)
            .order_by(Commissioner.from_year)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Return tz-aware UTC now — used as a fallback for ``last_updated``."""
    from datetime import UTC

    return datetime.now(UTC)
