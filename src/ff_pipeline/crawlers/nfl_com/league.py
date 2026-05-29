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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from sqlalchemy import select

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
    parse_transactions,
    parse_weekly_matchups,
)
from ff_pipeline.crawlers.nfl_com.urls import (
    league_home,
    owners,
    team_home,
    transactions,
    weekly_matchups,
)
from ff_pipeline.logging_config import get_logger
from ff_pipeline.normalizer.player_ids import PlayerIdentity, PlayerResolver
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
from ff_pipeline.repository.upsert import upsert

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

log = get_logger(__name__)

SOURCE_NAME = "nfl_com"
SnapshotKind = Literal["pre_kickoff", "audit"]


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

        # --- Transactions (whole season log) ---
        txn_html = fetcher.get_html(transactions(league_id, year))
        parsed_txns = parse_transactions(txn_html)
        txn_counts = _upsert_transactions(
            session,
            season_id=season_id,
            season_year=year,
            parsed=parsed_txns,
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
    upsert(
        session,
        Season,
        [{"league_id": league_id, "year": year, "status": "in_progress"}],
        conflict_cols=("league_id", "year"),
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
        row.display_name: row
        for row in session.execute(select(Owner).where(Owner.league_id == league_id)).scalars()
    }
    for parsed_owner in parsed:
        owner = existing.get(parsed_owner.display_name)
        if owner is None:
            owner = Owner(
                league_id=league_id,
                display_name=parsed_owner.display_name,
                nfl_user_id=parsed_owner.nfl_user_id,
                is_active=True,
            )
            session.add(owner)
            session.flush()
            owners_added += 1
        else:
            changed = False
            if parsed_owner.nfl_user_id and owner.nfl_user_id != parsed_owner.nfl_user_id:
                owner.nfl_user_id = parsed_owner.nfl_user_id
                changed = True
            if changed:
                owners_updated += 1
        if parsed_owner.team_id is not None:
            owner_id_by_team_id[parsed_owner.team_id] = owner.owner_id

    # Teams: one row per (season_id, owner_id). The NFL.com team_id is
    # not part of the table — we stash it in team_abbrev only as a hint
    # when present.
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
    rows = []
    for p in parsed:
        owner_id = owner_id_by_team_id.get(p.team_id) if p.team_id is not None else None
        if owner_id is None:
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
        return _Counts(0, 0)
    counts = upsert(session, Team, rows, conflict_cols=("season_id", "owner_id"))
    return _Counts(counts.rows_added, counts.rows_updated)


def _team_id_lookup(session: Session, season_id: int) -> dict[int, int]:
    """Map NFL.com team_id (stashed in team_abbrev) → internal teams.team_id.

    Returns an empty dict if no abbrevs were populated (e.g., owners
    page didn't expose /team/{id} hrefs).
    """
    rows = session.execute(
        select(Team.team_id, Team.team_abbrev).where(Team.season_id == season_id)
    ).all()
    out: dict[int, int] = {}
    for team_id, abbrev in rows:
        if not abbrev:
            continue
        try:
            out[int(abbrev)] = team_id
        except ValueError:
            continue
    return out


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
    _ = year  # roster is fetched as "current", not year-tagged — kept for symmetry
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
                },
            }
        )
    if not rows:
        return _Counts(0, 0)
    counts = upsert(
        session,
        TeamRoster,
        rows,
        conflict_cols=("team_id", "player_id", "week"),
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
        ).where(Transaction.season_id == season_id)
    ).all()
    for row in existing:
        fingerprints_seen.add((row[0], row[1], row[2], row[3], _fingerprint_dt(row[4])))

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
        fingerprint = (
            t.transaction_type,
            team_id,
            player_id,
            t.direction,
            _fingerprint_dt(executed_at),
        )
        if fingerprint in fingerprints_seen:
            skipped += 1
            continue
        fingerprints_seen.add(fingerprint)
        rows_to_insert.append(
            {
                "season_id": season_id,
                "transaction_type": t.transaction_type,
                "executed_at": executed_at,
                "effective_week": t.effective_week,
                "team_id": team_id,
                "counterpart_team_id": None,
                "player_id": player_id,
                "direction": t.direction,
                "notes": t.notes,
            }
        )
    if rows_to_insert:
        for new_row in rows_to_insert:
            session.add(Transaction(**new_row))
        inserted = len(rows_to_insert)
    _ = warnings  # reserved hook
    return _Counts(inserted, skipped)


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
