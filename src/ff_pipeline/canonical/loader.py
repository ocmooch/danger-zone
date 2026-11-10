"""Write a normalized season (``SeasonBundle``) into the canonical tables (S5).

The only writer of platform league facts. Every entity is *synced*: rows are
matched on a natural key, updated in place when a value changed, inserted when
new, and removed when stale. Re-running a load therefore changes nothing, and
surrogate ids stay stable across reloads. The caller commits.

Order: season → teams → matchups → rosters (draft week 0 + weekly) →
transactions → brackets, season results, traded picks (S7) → records →
rostered spans → owners recompute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy import select

from ff_pipeline.repository.legacy_guard import SealedSeasonError, assert_writable
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.repository.models import (
    Asset,
    LegacySealedRow,
    Matchup,
    Owner,
    PlayoffBracket,
    Season,
    Team,
    TeamRoster,
    TradedPick,
    Transaction,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.canonical.dto import SeasonBundle

__all__ = ["LoadResult", "PlatformMismatchError", "SealedSeasonError", "load_season"]

K = TypeVar("K")


class PlatformMismatchError(RuntimeError):
    """The season row for this year belongs to another platform (D-2 not applied)."""


@dataclass(slots=True)
class Counts:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0

    def __str__(self) -> str:
        return f"+{self.inserted} ~{self.updated} -{self.deleted} ={self.unchanged}"


@dataclass(slots=True)
class LoadResult:
    season_id: int = 0
    counts: dict[str, Counts] = field(default_factory=dict)
    record_mismatches: list[str] = field(default_factory=list)
    owner_changes: list[str] = field(default_factory=list)  # written (unsealed owners)
    owner_blocked: list[str] = field(default_factory=list)  # sealed owners: needs amend
    spans_refreshed: int = 0
    warnings: list[str] = field(default_factory=list)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """DB datetimes are naive UTC (as NFL.com rows); compare like with like."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _sync(
    session: Session,
    model: type[Any],
    existing: dict[K, Any],
    incoming: dict[K, dict[str, Any]],
    *,
    delete_stale: bool = True,
) -> Counts:
    c = Counts()
    if delete_stale:
        for key in [k for k in existing if k not in incoming]:
            session.delete(existing.pop(key))
            c.deleted += 1
        session.flush()
    for key, values in incoming.items():
        row = existing.get(key)
        if row is None:
            session.add(model(**values))
            c.inserted += 1
            continue
        changed = False
        for col, val in values.items():
            current = getattr(row, col)
            if col == "executed_at":
                current = _naive_utc(current)
            if current != val:
                setattr(row, col, val)
                changed = True
        if changed:
            c.updated += 1
        else:
            c.unchanged += 1
    session.flush()
    return c


def load_season(
    session: Session, bundle: SeasonBundle, *, league_id: str, platform: str = "sleeper"
) -> LoadResult:
    res = LoadResult(warnings=list(bundle.warnings))
    season = _season(session, bundle, league_id=league_id, platform=platform)
    res.season_id = season.season_id
    assert_writable(session, season.season_id)

    avatar_ids = _avatar_ids(session, bundle)

    # teams
    teams: dict[str | None, Team] = {
        t.platform_team_key: t
        for t in session.scalars(
            select(Team).where(
                Team.season_id == season.season_id, Team.platform_team_key.is_not(None)
            )
        )
    }
    others = session.scalars(
        select(Team).where(Team.season_id == season.season_id, Team.platform_team_key.is_(None))
    ).all()
    if others:
        raise PlatformMismatchError(
            f"season {season.year} still holds {len(others)} non-platform teams; "
            "quarantine them first (legacy quarantine-2026-stub)"
        )
    res.counts["teams"] = _sync(
        session,
        Team,
        teams,
        {
            cast("str | None", t.team_key): {
                "season_id": season.season_id,
                "platform_team_key": t.team_key,
                "owner_id": t.owner_id,
                "team_name": t.team_name,
                "team_abbrev": t.team_abbrev,
                "draft_position": t.draft_position,
                "division": t.division,
                "extra_data": t.extra_data,
                "final_rank": t.final_rank,
                "playoff_finish": t.playoff_finish,
                "made_playoffs": t.made_playoffs,
                "team_avatar_asset_id": avatar_ids.get(t.team_avatar_url or ""),
                "owner_avatar_asset_id": avatar_ids.get(t.owner_avatar_url or ""),
            }
            for t in bundle.teams
        },
        delete_stale=False,
    )
    team_id = {
        t.platform_team_key: t.team_id
        for t in session.scalars(select(Team).where(Team.season_id == season.season_id))
        if t.platform_team_key
    }

    def tid(key: str | None) -> int | None:
        return team_id[key] if key is not None else None

    # matchups
    existing_m = {
        (m.week, m.team_id): m
        for m in session.scalars(select(Matchup).where(Matchup.season_id == season.season_id))
    }
    res.counts["matchups"] = _sync(
        session,
        Matchup,
        existing_m,
        {
            (m.week, team_id[m.team_key]): {
                "season_id": season.season_id,
                "week": m.week,
                "team_id": team_id[m.team_key],
                "opponent_team_id": tid(m.opponent_key),
                "team_score": m.team_score,
                "opponent_score": m.opponent_score,
                "is_win": _is_win(m.team_score, m.opponent_score) if m.finalized else None,
                "is_playoff": m.is_playoff,
                "is_consolation": m.is_consolation,
                "platform_matchup_key": m.platform_matchup_key,
                "extra_data": m.extra_data,
            }
            for m in bundle.matchups
        },
        delete_stale=False,  # a load never removes weeks it did not see
    )

    # rosters: one owner per player per week (UNIQUE season_year, week, player_id)
    season_team_ids = set(team_id.values())
    roster_total = Counts()
    for week in sorted({r.week for r in bundle.rosters}):
        existing_r = {
            r.player_id: r
            for r in session.scalars(
                select(TeamRoster).where(
                    TeamRoster.season_year == season.year,
                    TeamRoster.week == week,
                    TeamRoster.team_id.in_(season_team_ids),
                )
            )
        }
        c = _sync(
            session,
            TeamRoster,
            existing_r,
            {
                r.player_id: {
                    "team_id": team_id[r.team_key],
                    "player_id": r.player_id,
                    "season_year": season.year,
                    "week": r.week,
                    "roster_slot": r.roster_slot,
                    "is_starter": r.is_starter,
                    "was_locked_at_kickoff": r.was_locked_at_kickoff,
                    "extra_data": r.extra_data,
                }
                for r in bundle.rosters
                if r.week == week
            },
        )
        for attr in ("inserted", "updated", "deleted", "unchanged"):
            setattr(roster_total, attr, getattr(roster_total, attr) + getattr(c, attr))
    res.counts["team_rosters"] = roster_total

    # transactions (platform rows only; keyed like the S3 partial unique index)
    key_of_team = {v: k for k, v in team_id.items()}
    existing_t: dict[tuple[str, int, str, str], Transaction] = {
        (
            t.platform_transaction_id or "",
            t.player_id if t.player_id is not None else -1,
            key_of_team.get(t.team_id or -1, ""),
            t.direction or "",
        ): t
        for t in session.scalars(
            select(Transaction).where(
                Transaction.season_id == season.season_id,
                Transaction.platform_transaction_id.is_not(None),
            )
        )
    }
    res.counts["transactions"] = _sync(
        session,
        Transaction,
        existing_t,
        {
            leg.key: {
                "season_id": season.season_id,
                "platform_transaction_id": leg.platform_transaction_id,
                "transaction_type": leg.transaction_type,
                "direction": leg.direction,
                "team_id": team_id[leg.team_key],
                "counterpart_team_id": tid(leg.counterpart_key),
                "player_id": leg.player_id,
                "executed_at": _naive_utc(leg.executed_at),
                "effective_week": leg.effective_week,
                "status": leg.status,
                "waiver_priority_used": leg.waiver_priority_used,
                "notes": leg.notes,
                "extra_data": leg.extra_data,
            }
            for leg in bundle.transactions
        },
    )

    _load_postseason(session, season, bundle, team_id, res)
    res.counts["traded_picks"] = _load_traded_picks(session, season, bundle, league_id)

    res.record_mismatches = _records(session, season, bundle, team_id)
    res.spans_refreshed = recompute_rostered_spans(session)
    res.owner_changes, res.owner_blocked = _recompute_owners(session, league_id)
    session.flush()
    return res


def _avatar_ids(session: Session, bundle: SeasonBundle) -> dict[str, int]:
    """Asset ids for the avatar URLs the capture already downloaded (S7-D8)."""
    urls = {u for t in bundle.teams for u in (t.team_avatar_url, t.owner_avatar_url) if u}
    if not urls:
        return {}
    return {
        str(url): int(aid)
        for url, aid in session.execute(
            select(Asset.source_url, Asset.asset_id).where(Asset.source_url.in_(urls))
        ).tuples()
    }


def _load_postseason(
    session: Session,
    season: Season,
    bundle: SeasonBundle,
    team_id: dict[str, int],
    res: LoadResult,
) -> None:
    def tid(key: str | None) -> int | None:
        return team_id.get(key) if key is not None else None

    existing = {
        (b.bracket, b.round, b.match_no): b
        for b in session.scalars(
            select(PlayoffBracket).where(PlayoffBracket.season_id == season.season_id)
        )
    }
    res.counts["playoff_brackets"] = _sync(
        session,
        PlayoffBracket,
        existing,
        {
            (b.bracket, b.round, b.match_no): {
                "season_id": season.season_id,
                "bracket": b.bracket,
                "round": b.round,
                "match_no": b.match_no,
                "team1_id": tid(b.team1_key),
                "team2_id": tid(b.team2_key),
                "winner_team_id": tid(b.winner_key),
                "loser_team_id": tid(b.loser_key),
                "placement": b.placement,
                "team1_from": b.team1_from,
                "team2_from": b.team2_from,
                "source": "sleeper",
            }
            for b in bundle.brackets
        },
    )
    r = bundle.results
    for col, key in (
        ("champion_team_id", r.champion_key),
        ("runner_up_team_id", r.runner_up_key),
        ("last_place_team_id", r.last_place_key),
    ):
        if getattr(season, col) != tid(key):
            setattr(season, col, tid(key))
    session.flush()


def _load_traded_picks(
    session: Session, season: Season, bundle: SeasonBundle, league_id: str
) -> Counts:
    """Current owners of traded future picks (pick seasons after this one), S7-D6."""
    owner_of = {t.team_key: t.owner_id for t in bundle.teams}
    existing = {
        (p.pick_season, p.round, p.original_owner_id): p
        for p in session.scalars(
            select(TradedPick).where(
                TradedPick.league_id == league_id, TradedPick.pick_season > season.year
            )
        )
    }
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    incoming: dict[tuple[int, int, int], dict[str, Any]] = {}
    for p in bundle.traded_picks:
        if p.pick_season <= season.year or p.original_key not in owner_of:
            continue
        key = (p.pick_season, p.round, owner_of[p.original_key])
        current = owner_of.get(p.current_key)
        old = existing.get(key)
        incoming[key] = {
            "league_id": league_id,
            "pick_season": p.pick_season,
            "round": p.round,
            "original_owner_id": key[2],
            "current_owner_id": current,
            "previous_owner_id": owner_of.get(p.previous_key) if p.previous_key else None,
            # first seen with its current owner
            "observed_at": (
                old.observed_at if old is not None and old.current_owner_id == current else now
            ),
            "source_transaction_id": p.source_transaction_id,
            "extra_data": {"original_team_key": p.original_key, "current_team_key": p.current_key},
        }
    counts = _sync(session, TradedPick, existing, incoming)
    return counts


def _season(session: Session, bundle: SeasonBundle, *, league_id: str, platform: str) -> Season:
    dto = bundle.season
    season = session.scalars(
        select(Season).where(Season.league_id == league_id, Season.year == dto.year)
    ).first()
    if season is None:
        season = Season(league_id=league_id, year=dto.year, platform=platform)
        session.add(season)
        session.flush()
    if season.is_sealed:
        raise SealedSeasonError(season.year, season.seal_id)
    if season.platform != platform:
        raise PlatformMismatchError(
            f"season {dto.year} is platform {season.platform!r}, not {platform!r}"
        )
    assert_writable(session, season.season_id)
    for col, val in (
        ("status", dto.status),
        ("regular_season_weeks", dto.regular_season_weeks),
        ("playoff_weeks", dto.playoff_weeks),
        ("platform_league_id", dto.platform_league_id),
        ("platform_draft_id", dto.platform_draft_id),
        ("settings_snapshot", dto.settings_snapshot),
    ):
        if getattr(season, col) != val:
            setattr(season, col, val)
    session.flush()
    return season


def _is_win(score: float | None, opp: float | None) -> bool | None:
    if score is None or opp is None or score == opp:
        return None
    return score > opp


def _records(
    session: Session, season: Season, bundle: SeasonBundle, team_id: dict[str, int]
) -> list[str]:
    """Regular-season W-L-T / PF / PA from finalized weeks; cross-check vs the platform."""
    reg_weeks = bundle.season.regular_season_weeks or 10**6
    agg: dict[str, list[float]] = {t.team_key: [0, 0, 0, 0.0, 0.0] for t in bundle.teams}
    for m in bundle.matchups:
        if not m.finalized or m.week > reg_weeks or m.opponent_key is None:
            continue
        a = agg[m.team_key]
        ts, os = m.team_score or 0.0, m.opponent_score or 0.0
        a[0 if ts > os else 1 if ts < os else 2] += 1
        a[3] += ts
        a[4] += os
    mismatches: list[str] = []
    for t in bundle.teams:
        w, l_, ti, pf, pa = agg[t.team_key]
        team = session.get(Team, team_id[t.team_key])
        assert team is not None
        values = {
            "regular_season_wins": int(w),
            "regular_season_losses": int(l_),
            "regular_season_ties": int(ti),
            "regular_season_points_for": round(pf, 2),
            "regular_season_points_against": round(pa, 2),
        }
        for col, val in values.items():
            if getattr(team, col) != val:
                setattr(team, col, val)
        if t.platform_record is not None:
            mine = (int(w), int(l_), int(ti), round(pf, 2), round(pa, 2))
            theirs = t.platform_record
            if (
                mine[:3] != theirs[:3]
                or abs(mine[3] - theirs[3]) > 0.011
                or abs(mine[4] - theirs[4]) > 0.011
            ):
                mismatches.append(f"{t.team_key}: derived {mine} vs platform {theirs}")
    _ = season
    return mismatches


def _recompute_owners(session: Session, league_id: str) -> tuple[list[str], list[str]]:
    """is_active / joined_year / left_year across all eras (S5-D8).

    Writes only owners outside the seal; a needed change to a sealed owner is
    reported, never forced (it needs ``legacy amend``).
    """
    rows = session.execute(
        select(Team.owner_id, Season.year)
        .join(Season, Season.season_id == Team.season_id)
        .where(Season.league_id == league_id)
    ).all()
    if not rows:
        return [], []
    latest = max(y for _, y in rows)
    years: dict[int, list[int]] = {}
    for owner_id, year in rows:
        years.setdefault(int(owner_id), []).append(int(year))
    sealed = set(
        session.scalars(select(LegacySealedRow.pk).where(LegacySealedRow.table_name == "owners"))
    )
    written: list[str] = []
    blocked: list[str] = []
    for owner_id, ys in sorted(years.items()):
        owner = session.get(Owner, owner_id)
        if owner is None:
            continue
        active = latest in ys
        want = {
            "is_active": active,
            "joined_year": min(ys),
            "left_year": None if active else max(ys),
        }
        diff = {k: v for k, v in want.items() if getattr(owner, k) != v}
        if not diff:
            continue
        if str(owner_id) in sealed:
            blocked.append(f"owner {owner_id}: {diff} (sealed; needs legacy amend)")
            continue
        for k, v in diff.items():
            setattr(owner, k, v)
        written.append(f"owner {owner_id}: {diff}")
    return written, blocked
