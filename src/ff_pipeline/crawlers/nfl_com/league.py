"""High-level "scrape this season's data from NFL.com" entry point.

Orchestrates the lower-level parsers + HTTP client + DB upserter to
populate:

* ``leagues``, ``seasons``, ``owners``, ``teams`` (league shape)
* ``team_rosters`` for every team for the target week
* ``matchups`` for the target week
* ``transactions`` for the season (full log every run — idempotent)
* ``player_availability`` for the target week
* ``pipeline_runs`` + ``source_health`` rows

Game-time snapshot logic
------------------------

NFL.com locks lineups at game time (Sunday 1pm ET for most slots).
``is_pre_kickoff_snapshot=True`` rows are the authoritative "this is
the state at kickoff" record per (player, week). The runner accepts a
``snapshot_kind`` argument:

* ``snapshot_kind="pre_kickoff"`` — emitted by a Sunday morning sync
  before kickoff. The rows it writes have ``is_pre_kickoff_snapshot=True``.
* ``snapshot_kind="audit"`` — emitted by mid-week / post-kickoff syncs.
  The rows it writes have ``is_pre_kickoff_snapshot=False``, so the
  audit trail accumulates without overwriting the canonical pre-kickoff
  row.

When ``snapshot_kind`` is not provided, the runner derives it from the
current UTC time via ``_default_snapshot_kind``: Sunday before 18:00 UTC
(approx 1pm ET kickoff) is pre-kickoff; everything else is audit.

Audit captures are NOT week-accurate
------------------------------------

The live NFL.com roster page is *not* week-aware — it always returns
*today's* roster. ``run_nfl_com(year, week=N, ...)`` writes that current
roster into the ``week=N`` slot. An ``audit`` sync run mid/late-season but
pointed at an early week would therefore stamp the *current* rosters onto
that early week (this is the 2025/2026 week-1 corruption, where every team's
week-1 roster was overwritten with its end-of-season roster).

To prevent recurrence, ``_scrape_and_upsert_rosters`` quarantines an audit
roster write whenever the target (season, week) already holds an
authoritative snapshot (``AUTHORITATIVE_SNAPSHOT_KINDS`` — draft /
pre_kickoff / history): the roster write is skipped so an audit capture can
never become a week's sole authoritative roster.

Remediating existing bad rows: prevention does not repair rows already in
the DB (2025 wk1, 2026 wk1 — entirely ``audit``). Overwrite them by
re-running the history reconstruction for that week, which owns the whole
(season, week) snapshot and replaces every row with week-accurate
``history`` data::

    uv run ff-pipeline reconstruct --year 2025   # or the per-week path in
    # ff_pipeline.crawlers.nfl_com.history.reconstruct_lineups(weeks=[1])

(Requires live NFL.com access; do not run offline.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import delete, func, select, update

from ff_pipeline.crawlers.nfl_com.availability import sweep_availability
from ff_pipeline.crawlers.nfl_com.client import (
    AuthFailureError,
    NflComClient,
    NflComClientError,
)
from ff_pipeline.crawlers.nfl_com.parsers import (
    ParsedAvailability,
    ParsedMatchup,
    ParsedOwner,
    ParsedTeamRoster,
    ParsedTransaction,
    parse_league_home,
    parse_owners,
    parse_team_roster,
    parse_weekly_matchups,
)
from ff_pipeline.crawlers.nfl_com.transactions import sweep_transactions
from ff_pipeline.crawlers.nfl_com.urls import (
    league_home,
    owners,
    team_home,
    weekly_matchups,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.normalizer.player_ids import PlayerIdentity, PlayerResolver
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    PipelineRun,
    PlayerAvailability,
    Season,
    SourceHealth,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.owner_identities import canonicalize_owner_identity
from ff_pipeline.repository.upsert import upsert

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

log = get_logger(__name__)

SOURCE_NAME = "nfl_com"
SnapshotKind = Literal["pre_kickoff", "audit"]

# Snapshot kinds that are *week-accurate* and therefore authoritative for a
# given (season, week): the draft (week 0), the live pre-kickoff lock, and the
# post-hoc history reconstruction. An ``audit`` capture is NOT week-accurate —
# the live NFL.com roster page is not week-aware and always returns *today's*
# roster (see ``_default_snapshot_kind``), so an audit sync pointed at an early
# week would otherwise stamp the current roster onto that week. ``audit`` may
# only fill a (season, week) that has no authoritative snapshot yet; it must
# never overwrite one. Enforced in ``_scrape_and_upsert_rosters``.
AUTHORITATIVE_SNAPSHOT_KINDS: frozenset[str] = frozenset({"pre_kickoff", "draft", "history"})


class _HtmlFetcher(Protocol):
    """Protocol that ``NflComClient`` (and test stubs) satisfy.

    Defined here so callers can pass a stub without depending on the
    httpx-backed client.
    """

    def get_html(self, url: str) -> str: ...


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NflComRunResult:
    """Aggregate counts surfaced to the CLI + ``pipeline_runs.sources_summary``."""

    owners_added: int = 0
    owners_updated: int = 0
    teams_added: int = 0
    teams_updated: int = 0
    rosters_added: int = 0
    rosters_updated: int = 0
    matchups_added: int = 0
    matchups_updated: int = 0
    transactions_added: int = 0
    transactions_updated: int = 0
    availability_added: int = 0
    availability_updated: int = 0
    snapshot_kind: SnapshotKind = "audit"
    duration_ms: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_nfl_com(
    session: Session,
    *,
    league_id: str,
    year: int,
    week: int,
    fetcher: _HtmlFetcher,
    snapshot_kind: SnapshotKind | None = None,
    now: datetime | None = None,
    mode: str = "full_sync",
) -> NflComRunResult:
    """Scrape ``league_id``'s state for ``(year, week)`` into the DB.

    Caller commits. On exception, marks ``pipeline_runs`` failed and
    writes a ``source_health`` row, then re-raises so the CLI exits non-zero.
    """
    effective_snapshot = snapshot_kind or _default_snapshot_kind(now or datetime.now(tz=UTC))

    run = PipelineRun(status="running", mode=mode)
    session.add(run)
    session.flush()
    start = time.perf_counter()
    warnings: list[str] = []

    try:
        # --- League shape ---
        league_html = fetcher.get_html(league_home(league_id))
        league_parsed = parse_league_home(league_html)

        _upsert_league(session, league_parsed)
        season_id = _upsert_season(session, league_id, year)
        session.flush()

        # One PlayerResolver per run — caches every player lookup
        # within the run so the per-row roster/transaction/availability
        # helpers don't re-SELECT each time.
        resolver = PlayerResolver(session)

        # --- Owners + teams ---
        owners_html = fetcher.get_html(owners(league_id))
        parsed_owners = parse_owners(owners_html)
        owner_counts, team_counts = _upsert_owners_and_teams(
            session, league_id=league_id, season_id=season_id, parsed=parsed_owners
        )
        session.flush()
        team_id_by_nfl_team_id = _team_id_lookup(session, season_id)

        # --- Rosters per team (one HTTP fetch per team) ---
        roster_counts = _scrape_and_upsert_rosters(
            session,
            fetcher=fetcher,
            league_id=league_id,
            year=year,
            week=week,
            team_id_by_nfl_team_id=team_id_by_nfl_team_id,
            snapshot_kind=effective_snapshot,
            warnings=warnings,
            resolver=resolver,
        )

        # --- Matchups ---
        matchups_html = fetcher.get_html(weekly_matchups(league_id, year, week))
        parsed_matchups = parse_weekly_matchups(matchups_html)
        matchup_counts = _upsert_matchups(
            session,
            season_id=season_id,
            week=week,
            parsed=parsed_matchups,
            team_id_by_nfl_team_id=team_id_by_nfl_team_id,
            warnings=warnings,
        )

        # --- Transactions (whole season log, every page) ---
        txn_sweep = sweep_transactions(fetcher, league_id=league_id, year=year)
        txn_counts = _upsert_transactions(
            session,
            season_id=season_id,
            season_year=year,
            parsed=txn_sweep.rows,
            team_id_by_nfl_team_id=team_id_by_nfl_team_id,
            warnings=warnings,
            resolver=resolver,
        )

        # --- League-wide availability sweep ---
        sweep = sweep_availability(fetcher, league_id=league_id, year=year, week=week)
        avail_counts = _upsert_availability(
            session,
            year=year,
            week=week,
            parsed=sweep.rows,
            team_id_by_nfl_team_id=team_id_by_nfl_team_id,
            snapshot_kind=effective_snapshot,
            resolver=resolver,
        )

        # The roster write above may have added a player's first/last appearance
        # in this league; refresh the materialized league-relevance span so the
        # read API's "rostered 2012-2018" / league-relevant filter stays current.
        recompute_rostered_spans(session)

    except AuthFailureError as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.error_summary = f"AuthFailureError: {exc}"
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source=SOURCE_NAME,
                status="auth_failure",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        )
        raise
    except (NflComClientError, Exception) as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        run.status = "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.error_summary = f"{type(exc).__name__}: {exc}"
        session.add(
            SourceHealth(
                run_id=run.run_id,
                source=SOURCE_NAME,
                status="failed",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        )
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    result = NflComRunResult(
        owners_added=owner_counts.rows_added,
        owners_updated=owner_counts.rows_updated,
        teams_added=team_counts.rows_added,
        teams_updated=team_counts.rows_updated,
        rosters_added=roster_counts[0],
        rosters_updated=roster_counts[1],
        matchups_added=matchup_counts.rows_added,
        matchups_updated=matchup_counts.rows_updated,
        transactions_added=txn_counts.rows_added,
        transactions_updated=txn_counts.rows_updated,
        availability_added=avail_counts.rows_added,
        availability_updated=avail_counts.rows_updated,
        snapshot_kind=effective_snapshot,
        duration_ms=duration_ms,
        warnings=tuple(warnings),
    )
    run.status = "success" if not warnings else "partial_success"
    run.finished_at = datetime.now(tz=UTC)
    run.sources_summary = {
        SOURCE_NAME: {
            "year": year,
            "week": week,
            "snapshot_kind": effective_snapshot,
            "owners_added": result.owners_added,
            "owners_updated": result.owners_updated,
            "teams_added": result.teams_added,
            "teams_updated": result.teams_updated,
            "rosters_added": result.rosters_added,
            "rosters_updated": result.rosters_updated,
            "matchups_added": result.matchups_added,
            "matchups_updated": result.matchups_updated,
            "transactions_added": result.transactions_added,
            "transactions_updated": result.transactions_updated,
            "availability_added": result.availability_added,
            "availability_updated": result.availability_updated,
            "warnings": list(warnings),
        }
    }
    session.add(
        SourceHealth(
            run_id=run.run_id,
            source=SOURCE_NAME,
            status="success" if not warnings else "skipped",
            rows_added=(
                result.owners_added
                + result.teams_added
                + result.rosters_added
                + result.matchups_added
                + result.transactions_added
                + result.availability_added
            ),
            rows_updated=(
                result.owners_updated
                + result.teams_updated
                + result.rosters_updated
                + result.matchups_updated
                + result.transactions_updated
                + result.availability_updated
            ),
            parse_failures=len(warnings),
            duration_ms=duration_ms,
        )
    )

    log.info(
        "nfl_com run complete",
        league_id=league_id,
        year=year,
        week=week,
        snapshot_kind=effective_snapshot,
        duration_ms=duration_ms,
        warnings=warnings,
    )
    return result


# ---------------------------------------------------------------------------
# Snapshot-kind heuristic
# ---------------------------------------------------------------------------


def _default_snapshot_kind(now: datetime) -> SnapshotKind:
    """Decide if NOW is "pre-kickoff" or "audit".

    Pre-kickoff window: Sunday (weekday 6) before 18:00 UTC (~13:00 ET
    kickoff). Everything else — including Thursday-night and Monday-night
    syncs done after their respective kickoffs — is "audit" so the
    pre-kickoff snapshot row for the Sunday majority is not overwritten.
    Callers who need a different definition pass ``snapshot_kind`` explicitly.
    """
    if now.weekday() == 6 and now.hour < 18:
        return "pre_kickoff"
    return "audit"


# ---------------------------------------------------------------------------
# Helpers: league / season / owners / teams
# ---------------------------------------------------------------------------


def _upsert_league(session: Session, parsed: object) -> None:
    # The parsed object is ParsedLeagueHome but typed as object so the
    # type-checker doesn't grumble about the runner's dataclass cross-imports.
    from ff_pipeline.crawlers.nfl_com.parsers import ParsedLeagueHome

    assert isinstance(parsed, ParsedLeagueHome)
    upsert(
        session,
        League,
        [
            {
                "league_id": parsed.league_id,
                "name": parsed.league_name,
                "platform": "nfl_com",
                "current_season_year": parsed.current_season_year,
            }
        ],
        conflict_cols=("league_id",),
    )


def _upsert_season(session: Session, league_id: str, year: int) -> int:
    # Seed a freshly-created season as ``in_progress``; never touch the
    # status of an existing row. ``reconstruct_standings`` is the sole
    # authority that promotes a season to ``completed``, and a re-sync of
    # a finished season must not regress it (update_cols=() → DO NOTHING
    # on conflict).
    upsert(
        session,
        Season,
        [{"league_id": league_id, "year": year, "status": "in_progress"}],
        conflict_cols=("league_id", "year"),
        update_cols=(),
    )
    session.flush()
    season_id = session.execute(
        select(Season.season_id).where(Season.league_id == league_id, Season.year == year)
    ).scalar_one()
    return season_id


def _upsert_owners_and_teams(
    session: Session,
    *,
    league_id: str,
    season_id: int,
    parsed: list[ParsedOwner],
) -> tuple[_Counts, _Counts]:
    # Owners are keyed by display_name within a league (the NFL.com user
    # ID, if present, is opaque and not always exposed in the markup).
    # For idempotency we upsert into owners on (league_id, display_name)
    # by reading existing rows + writing the diff.
    owners_added = 0
    owners_updated = 0
    owner_id_by_team_id: dict[int, int] = {}

    existing = {
        row.display_name.casefold() if row.display_name else "": row
        for row in session.execute(select(Owner).where(Owner.league_id == league_id)).scalars()
    }
    for parsed_owner in parsed:
        identity = canonicalize_owner_identity(
            session,
            league_id=league_id,
            display_name=parsed_owner.display_name,
            nfl_user_id=parsed_owner.nfl_user_id,
        )
        owner = existing.get(identity.display_name.casefold())
        if owner is None:
            owner = Owner(
                league_id=league_id,
                display_name=identity.display_name,
                nfl_user_id=identity.nfl_user_id,
                aliases=_owner_aliases(None, identity.observed_display_name, identity.display_name),
                is_active=True,
            )
            session.add(owner)
            session.flush()
            existing[identity.display_name.casefold()] = owner
            owners_added += 1
        else:
            changed = False
            if owner.display_name != identity.display_name:
                owner.display_name = identity.display_name
                changed = True
            aliases = _owner_aliases(
                owner.aliases, identity.observed_display_name, identity.display_name
            )
            if aliases != owner.aliases:
                owner.aliases = aliases
                changed = True
            if identity.nfl_user_id and owner.nfl_user_id != identity.nfl_user_id:
                owner.nfl_user_id = identity.nfl_user_id
                changed = True
            if changed:
                owners_updated += 1
        if parsed_owner.team_id is not None:
            owner_id_by_team_id[parsed_owner.team_id] = owner.owner_id

    # Teams are keyed by their season-scoped rendered name. ``owner_id`` can
    # repeat when a manually canonicalized manager controlled multiple teams in
    # one season.
    team_counts = _upsert_teams(
        session,
        season_id=season_id,
        parsed=parsed,
        owner_id_by_team_id=owner_id_by_team_id,
    )

    return (
        _Counts(owners_added, owners_updated),
        team_counts,
    )


def _owner_aliases(
    existing: object,
    observed_display_name: str,
    canonical_display_name: str,
) -> list[str] | None:
    aliases = set(existing if isinstance(existing, list) else [])
    if observed_display_name != canonical_display_name:
        aliases.add(observed_display_name)
    return sorted(str(a) for a in aliases) or None


@dataclass(frozen=True, slots=True)
class _Counts:
    rows_added: int
    rows_updated: int


def _upsert_teams(
    session: Session,
    *,
    season_id: int,
    parsed: list[ParsedOwner],
    owner_id_by_team_id: dict[int, int],
) -> _Counts:
    existing_by_nfl_team_id = _team_id_lookup(session, season_id)
    added = 0
    updated = 0
    rows = []
    for p in parsed:
        owner_id = owner_id_by_team_id.get(p.team_id) if p.team_id is not None else None
        if owner_id is None:
            continue
        if p.team_id is not None and p.team_id in existing_by_nfl_team_id:
            team = session.get(Team, existing_by_nfl_team_id[p.team_id])
            if team is None:
                continue
            changed = False
            if team.owner_id != owner_id:
                team.owner_id = owner_id
                changed = True
            if team.team_name != p.team_name:
                team.team_name = p.team_name
                changed = True
            abbrev = str(p.team_id)
            if team.team_abbrev != abbrev:
                team.team_abbrev = abbrev
                changed = True
            if changed:
                updated += 1
            continue
        rows.append(
            {
                "season_id": season_id,
                "owner_id": owner_id,
                "team_name": p.team_name,
                "team_abbrev": str(p.team_id) if p.team_id is not None else None,
            }
        )
    if not rows:
        return _Counts(added, updated)
    counts = upsert(session, Team, rows, conflict_cols=("season_id", "team_name"))
    return _Counts(added + counts.rows_added, updated + counts.rows_updated)


def _team_id_lookup(session: Session, season_id: int) -> dict[int, int]:
    """Map NFL.com team_id (stashed in team_abbrev) → internal teams.team_id.

    Returns an empty dict if no abbrevs were populated (e.g., owners
    page didn't expose /team/{id} hrefs). When more than one row shares an
    abbrev (a legacy franchise-duplicate artifact), the row with the most
    roster/matchup references wins, so re-runs deterministically update the
    real franchise row and never resurrect a phantom duplicate.
    """
    rows = session.execute(
        select(Team.team_id, Team.team_abbrev).where(Team.season_id == season_id)
    ).all()
    candidates: dict[int, list[int]] = {}
    for team_id, abbrev in rows:
        if not abbrev:
            continue
        try:
            candidates.setdefault(int(abbrev), []).append(team_id)
        except ValueError:
            continue
    return {
        nfl_id: _preferred_team_id(session, team_ids) for nfl_id, team_ids in candidates.items()
    }


def _preferred_team_id(session: Session, team_ids: list[int]) -> int:
    """Among rows sharing an abbrev, prefer the one with the most child rows."""
    if len(team_ids) == 1:
        return team_ids[0]
    scored: list[tuple[int, int]] = []
    for team_id in team_ids:
        roster_refs = session.scalar(
            select(func.count()).select_from(TeamRoster).where(TeamRoster.team_id == team_id)
        )
        matchup_refs = session.scalar(
            select(func.count()).select_from(Matchup).where(Matchup.team_id == team_id)
        )
        scored.append((int(roster_refs or 0) + int(matchup_refs or 0), team_id))
    return max(scored)[1]


# ---------------------------------------------------------------------------
# Helpers: rosters
# ---------------------------------------------------------------------------


def _scrape_and_upsert_rosters(
    session: Session,
    *,
    fetcher: _HtmlFetcher,
    league_id: str,
    year: int,
    week: int,
    team_id_by_nfl_team_id: dict[int, int],
    snapshot_kind: SnapshotKind,
    warnings: list[str],
    resolver: PlayerResolver,
) -> tuple[int, int]:
    # Precedence guard: an ``audit`` capture is the current (not week-accurate)
    # roster, because the live NFL.com roster page ignores the requested week.
    # If this (season, week) already holds an authoritative snapshot
    # (draft / pre_kickoff / history), an audit write would clobber it and the
    # week would read as today's rosters stamped onto an earlier week — exactly
    # the 2025/2026 week-1 corruption. Quarantine the audit roster write in that
    # case: skip it entirely so audit can never become a week's sole
    # authoritative roster. (Non-roster facets — matchups / transactions /
    # availability — are unaffected and still sync.)
    if snapshot_kind == "audit" and _week_has_authoritative_roster(
        session, season_year=year, week=week
    ):
        msg = (
            f"audit roster write skipped for week={week}: an authoritative "
            f"snapshot already exists; audit captures are not week-accurate and "
            f"must not overwrite draft/pre_kickoff/history rows"
        )
        log.info("roster audit write quarantined", year=year, week=week)
        warnings.append(msg)
        return 0, 0
    total_added = 0
    total_updated = 0
    for nfl_team_id, internal_team_id in team_id_by_nfl_team_id.items():
        try:
            html = fetcher.get_html(team_home(league_id, nfl_team_id))
            parsed = parse_team_roster(html)
        except Exception as exc:
            log.warning("team_roster fetch/parse failed", team_id=nfl_team_id, error=str(exc))
            warnings.append(f"team_roster team_id={nfl_team_id}: {exc}")
            continue
        counts = _upsert_team_roster(
            session,
            internal_team_id=internal_team_id,
            week=week,
            parsed=parsed,
            snapshot_kind=snapshot_kind,
            resolver=resolver,
        )
        total_added += counts.rows_added
        total_updated += counts.rows_updated
    return total_added, total_updated


def _week_has_authoritative_roster(session: Session, *, season_year: int, week: int) -> bool:
    """True if any roster row for ``(season_year, week)`` is week-accurate.

    Week-accurate kinds are ``AUTHORITATIVE_SNAPSHOT_KINDS`` (draft /
    pre_kickoff / history). ``snapshot_kind`` lives in ``extra_data`` JSON,
    which is read back as a Python dict, so we inspect the tag in Python rather
    than via a dialect-specific JSON path (this runs once per sync, not per
    row). Rows missing the tag (legacy / unexpected) are treated as
    non-authoritative so an audit sync can still seed an otherwise-empty week.
    """
    existing = session.execute(
        select(TeamRoster.extra_data).where(
            TeamRoster.season_year == season_year,
            TeamRoster.week == week,
        )
    ).scalars()
    return any(
        isinstance(extra, dict) and extra.get("snapshot_kind") in AUTHORITATIVE_SNAPSHOT_KINDS
        for extra in existing
    )


def _upsert_team_roster(
    session: Session,
    *,
    internal_team_id: int,
    week: int,
    parsed: ParsedTeamRoster,
    snapshot_kind: SnapshotKind,
    resolver: PlayerResolver,
) -> _Counts:
    rows = []
    season_year = _season_year_for_team(session, internal_team_id)
    for entry in parsed.entries:
        if entry.player_id is None:
            continue
        internal_player_id = _ensure_player(
            resolver,
            nfl_com_player_id=entry.player_id,
            name=entry.player_name,
            position=entry.position,
            nfl_team=entry.nfl_team,
        )
        rows.append(
            {
                "team_id": internal_team_id,
                "player_id": internal_player_id,
                "season_year": season_year,
                "week": week,
                "roster_slot": entry.roster_slot,
                "is_starter": entry.is_starter,
                "was_locked_at_kickoff": snapshot_kind == "pre_kickoff",
                "acquisition_type": None,  # filled in by transactions normalizer (M7)
                "extra_data": {
                    "snapshot_kind": snapshot_kind,
                    "game_status": entry.game_status,
                    "opponent": entry.opponent,
                    "player_status": entry.player_status,
                    "player_status_label": entry.player_status_label,
                },
            }
        )
    if not rows:
        return _Counts(0, 0)
    # Replace-per-scope: clear this team's existing roster for the (season,
    # week) before writing the fresh snapshot. Without this, a second ingest
    # of the same week accumulated a duplicate snapshot, and players DROPPED
    # between snapshots lingered as stale rows. Re-running a week must yield
    # exactly one snapshot.
    session.execute(
        delete(TeamRoster).where(
            TeamRoster.team_id == internal_team_id,
            TeamRoster.season_year == season_year,
            TeamRoster.week == week,
        )
    )
    # Conflict on (season_year, week, player_id) — the cross-team invariant.
    # A player who moved teams between snapshots conflicts with his stale row
    # on the OLD team (which our per-team DELETE above won't reach when the old
    # team isn't in this load) and gets UPDATEd onto the new team, rather than
    # being double-rostered.
    counts = upsert(
        session,
        TeamRoster,
        rows,
        conflict_cols=("season_year", "week", "player_id"),
    )
    return _Counts(counts.rows_added, counts.rows_updated)


def _season_year_for_team(session: Session, team_id: int) -> int:
    return session.execute(
        select(Season.year)
        .join(Team, Team.season_id == Season.season_id)
        .where(Team.team_id == team_id)
    ).scalar_one()


# ---------------------------------------------------------------------------
# Helpers: matchups
# ---------------------------------------------------------------------------


def _upsert_matchups(
    session: Session,
    *,
    season_id: int,
    week: int,
    parsed: list[ParsedMatchup],
    team_id_by_nfl_team_id: dict[int, int],
    warnings: list[str],
) -> _Counts:
    rows = []
    for m in parsed:
        team_id = team_id_by_nfl_team_id.get(m.team_id)
        if team_id is None:
            warnings.append(f"matchup: unknown team_id={m.team_id}")
            continue
        opp_id = (
            team_id_by_nfl_team_id.get(m.opponent_team_id)
            if m.opponent_team_id is not None
            else None
        )
        rows.append(
            {
                "season_id": season_id,
                "week": week,
                "team_id": team_id,
                "opponent_team_id": opp_id,
                "team_score": m.team_score,
                "opponent_score": m.opponent_score,
                "is_win": _is_win(m.team_score, m.opponent_score),
                "is_playoff": m.is_playoff,
                "is_consolation": m.is_consolation,
                "nfl_com_game_id": m.game_id,
            }
        )
    if not rows:
        return _Counts(0, 0)
    counts = upsert(
        session,
        Matchup,
        rows,
        conflict_cols=("season_id", "week", "team_id"),
    )
    return _Counts(counts.rows_added, counts.rows_updated)


def _is_win(score: float | None, opp: float | None) -> bool | None:
    if score is None or opp is None:
        return None
    if score == opp:
        return None
    return score > opp


# ---------------------------------------------------------------------------
# Helpers: transactions
# ---------------------------------------------------------------------------


def _upsert_transactions(
    session: Session,
    *,
    season_id: int,
    season_year: int,
    parsed: Iterable[ParsedTransaction],
    team_id_by_nfl_team_id: dict[int, int],
    warnings: list[str],
    resolver: PlayerResolver,
) -> _Counts:
    """Append-only style upsert.

    Transactions have no natural composite key (NFL.com surfaces a
    transaction id sometimes but not always). We use a synthetic
    "fingerprint" of the tuple (season_id, type, team, player, direction,
    executed_at) by relying on the existing rows: if a row with matching
    fields already exists, skip; otherwise insert.
    """
    inserted = 0
    skipped = 0
    rows_to_insert: list[dict[str, object]] = []
    fingerprints_seen: set[tuple[object, ...]] = set()
    # transaction_id + current extra_data of already-stored rows, keyed by the
    # same fingerprint, so a re-run can *enrich* a matched row in place (e.g.
    # backfill a ``faab_bid`` onto a waiver claim first ingested before the bid
    # was parsed) without inserting a duplicate leg.
    existing_by_fp: dict[tuple[object, ...], tuple[int, dict[str, Any] | None]] = {}
    faab_updates: list[tuple[int, dict[str, Any]]] = []

    # Read the season's existing fingerprints in one query. ``executed_at``
    # is normalized to a UTC-isoformat string for the fingerprint because
    # SQLite drops tzinfo on round-trip; comparing the parsed datetime to
    # the round-tripped one would fail and produce duplicates on re-run.
    existing = session.execute(
        select(
            Transaction.transaction_type,
            Transaction.team_id,
            Transaction.player_id,
            Transaction.direction,
            Transaction.executed_at,
            Transaction.extra_data,
            Transaction.transaction_id,
        ).where(Transaction.season_id == season_id)
    ).all()
    for row in existing:
        fingerprint = (row[0], row[1], row[2], row[3], _fingerprint_dt(row[4]), _extra_sig(row[5]))
        fingerprints_seen.add(fingerprint)
        existing_by_fp.setdefault(fingerprint, (row[6], row[5]))

    for t in parsed:
        team_id = team_id_by_nfl_team_id.get(t.team_id) if t.team_id is not None else None
        player_id = (
            _ensure_player(
                resolver,
                nfl_com_player_id=t.player_id,
                name=t.player_name,
            )
            if t.player_id
            else None
        )
        executed_at = _parse_iso_datetime(t.executed_at, season_year=season_year)
        counterpart_team_id = (
            team_id_by_nfl_team_id.get(t.counterpart_team_id)
            if t.counterpart_team_id is not None
            else None
        )
        fingerprint = (
            t.transaction_type,
            team_id,
            player_id,
            t.direction,
            _fingerprint_dt(executed_at),
            _extra_sig(t.extra_data),
        )
        if fingerprint in fingerprints_seen:
            skipped += 1
            _collect_faab_enrich(existing_by_fp.get(fingerprint), t.extra_data, faab_updates)
            continue
        fingerprints_seen.add(fingerprint)
        rows_to_insert.append(
            {
                "season_id": season_id,
                "transaction_type": t.transaction_type,
                "executed_at": executed_at,
                "effective_week": t.effective_week,
                "team_id": team_id,
                "counterpart_team_id": counterpart_team_id,
                "player_id": player_id,
                "direction": t.direction,
                "notes": t.notes,
                "extra_data": t.extra_data,
            }
        )
    if rows_to_insert:
        for new_row in rows_to_insert:
            session.add(Transaction(**new_row))
        inserted = len(rows_to_insert)
    for txn_id, merged_extra in faab_updates:
        session.execute(
            update(Transaction)
            .where(Transaction.transaction_id == txn_id)
            .values(extra_data=merged_extra)
        )
    if faab_updates:
        log.info("transactions faab_bid enriched", season_id=season_id, rows=len(faab_updates))
    _ = warnings  # reserved hook
    return _Counts(inserted, skipped)


def _collect_faab_enrich(
    existing: tuple[int, dict[str, Any] | None] | None,
    parsed_extra: dict[str, Any] | None,
    out: list[tuple[int, dict[str, Any]]],
) -> None:
    """Queue an in-place ``faab_bid`` backfill for a fingerprint-matched row.

    The append-only upsert otherwise *skips* a matched row, so a waiver claim
    first ingested before the bid parser existed would never gain its bid. When
    the freshly parsed leg carries a ``faab_bid`` the stored row lacks (or that
    differs), merge it onto the stored ``extra_data``. ``faab_bid`` is kept out
    of ``_extra_sig`` so adding it never splits the fingerprint into a duplicate
    leg; re-running once the bid is stored is a no-op (idempotent).
    """
    if existing is None or parsed_extra is None:
        return
    bid = parsed_extra.get("faab_bid")
    if bid is None:
        return
    txn_id, stored_extra = existing
    stored_extra = stored_extra or {}
    if stored_extra.get("faab_bid") == bid:
        return
    out.append((txn_id, {**stored_extra, "faab_bid": bid}))


def _extra_sig(extra_data: dict[str, object] | None) -> tuple[object, ...] | None:
    """Distinguishing detail from ``extra_data``, folded into the fingerprint.

    Two row families carry their only distinguishing content in ``extra_data``
    rather than the team/player/direction columns, so without this they would
    collapse on the fingerprint when they share a minute:

    * **lineup moves** — differ only by slot (from_slot/to_slot);
    * **setting/commish rows** — null team/player/direction, so the change
      description is what makes them distinct. Commish actions cluster heavily
      (league setup fires dozens in the same minute), so omitting this drops
      the vast majority of the diary.
    """
    if not extra_data:
        return None
    if "from_slot" in extra_data or "to_slot" in extra_data:
        return ("slot", extra_data.get("from_slot"), extra_data.get("to_slot"))
    if "description" in extra_data or "from" in extra_data or "to" in extra_data:
        return (
            "setting",
            extra_data.get("description"),
            extra_data.get("from"),
            extra_data.get("to"),
        )
    return None


def _fingerprint_dt(value: datetime | None) -> str | None:
    """Normalize a datetime for fingerprint equality across SQLite round-trips.

    Strip tzinfo (SQLite stores naive on round-trip) and microseconds
    (parsing rebuilds without them) so the new-side and stored-side
    keys compare equal.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def _parse_iso_datetime(text: str | None, *, season_year: int | None = None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    # Formats that include their own year — NFL.com's API-style strings.
    full_formats = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%b %d, %Y %I:%M %p",
        "%b %d, %Y",
    )
    for fmt in full_formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    # Fall back to year-less formats only when the caller can tell us
    # which season to attribute the row to (e.g. the runner has it).
    if season_year is not None:
        # The history page renders dates as "Dec 28, 10:01am" — no year,
        # lowercase am/pm with no space. Normalize before strptime.
        normalized = text.replace("am", "AM").replace("pm", "PM")
        no_year_formats = ("%b %d, %I:%M%p", "%b %d, %I:%M %p", "%b %d")
        for fmt in no_year_formats:
            try:
                parsed = datetime.strptime(normalized, fmt)
            except ValueError:
                continue
            return parsed.replace(year=season_year, tzinfo=UTC)
    return None


# ---------------------------------------------------------------------------
# Helpers: availability
# ---------------------------------------------------------------------------


def _upsert_availability(
    session: Session,
    *,
    year: int,
    week: int,
    parsed: Iterable[ParsedAvailability],
    team_id_by_nfl_team_id: dict[int, int],
    snapshot_kind: SnapshotKind,
    resolver: PlayerResolver,
) -> _Counts:
    is_pre_kickoff = snapshot_kind == "pre_kickoff"
    rows: list[dict[str, object]] = []
    for a in parsed:
        owning_team_id = (
            team_id_by_nfl_team_id.get(a.owning_team_id) if a.owning_team_id is not None else None
        )
        internal_player_id = _ensure_player(
            resolver,
            nfl_com_player_id=a.player_id,
            name=a.player_name,
            position=a.position,
            nfl_team=a.nfl_team,
        )
        rows.append(
            {
                "player_id": internal_player_id,
                "season_year": year,
                "week": week,
                "status": a.status,
                "owning_team_id": owning_team_id,
                "is_pre_kickoff_snapshot": is_pre_kickoff,
                "extra_data": {
                    "waiver_claim_deadline_raw": a.waiver_claim_deadline,
                    "snapshot_kind": snapshot_kind,
                },
            }
        )
    if not rows:
        return _Counts(0, 0)
    counts = upsert(
        session,
        PlayerAvailability,
        rows,
        conflict_cols=("player_id", "season_year", "week", "is_pre_kickoff_snapshot"),
    )
    return _Counts(counts.rows_added, counts.rows_updated)


# ---------------------------------------------------------------------------
# Helpers: player stub creation
# ---------------------------------------------------------------------------


def _ensure_player(
    resolver: PlayerResolver,
    *,
    nfl_com_player_id: str | None,
    name: str | None,
    position: str | None = None,
    nfl_team: str | None = None,
) -> int:
    """Resolve (or create) a ``players`` row for an NFL.com observation.

    Delegates to :class:`PlayerResolver` so cross-source ID merging and
    fuzzy matching happen here instead of being duplicated per call site.
    The resolver may match this observation against an existing
    nflverse-populated row (by name+position) and stamp the NFL.com ID
    onto it — that's the M7 cross-source join the deeper-dive docs
    promise.
    """
    if not nfl_com_player_id and not name:
        raise NflComClientError("_ensure_player called with neither nfl_com_player_id nor name")
    identity = PlayerIdentity(
        name_full=name or nfl_com_player_id or "(unknown)",
        position=position,
        nfl_team=nfl_team,
        nfl_com_player_id=nfl_com_player_id,
    )
    return resolver.resolve(identity, source="nfl_com")


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def build_default_client(cookie: str, delay_seconds: float = 2.0) -> NflComClient:
    """Convenience for the CLI: produces a configured ``NflComClient``."""
    return NflComClient(cookie=cookie, delay_seconds=delay_seconds)


__all__ = [
    "SOURCE_NAME",
    "NflComRunResult",
    "SnapshotKind",
    "build_default_client",
    "run_nfl_com",
]
