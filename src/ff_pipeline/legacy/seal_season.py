"""Seal a finished Sleeper season: the rolling seal (S9-D5, ``03`` §10).

``seal_season`` refuses unless every precondition holds, then, in one
transaction with the guard unlocked: flips ``is_sealed`` for the season and
writes the next seal baseline (``seal-vN``, kind ``season_close``) covering all
sealed seasons, with the guard-v2 Sleeper-era fingerprints. A named backup is
taken first (outside retention) and ``verify`` must pass before and after.

The vault side (plaintext seal directory, encryption, upload) is run by the CLI
through ``scripts/vault/vault_sync.sh`` once the DB seal has succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ff_pipeline.legacy import guard_v2
from ff_pipeline.legacy.amend import backup_database, next_seal_id
from ff_pipeline.legacy.fingerprint import scan_all, sealed_years
from ff_pipeline.legacy.seal import (
    LegacyError,
    git_head,
    now_text,
    relock,
    require_guard_schema,
    set_unlock,
    write_baseline,
)
from ff_pipeline.legacy.verify import current_seal, verify

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

CORRECTION_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class SeasonCloseFacts:
    """What the CLI gathers outside the DB connection (archive, reconcile)."""

    complete_observed_at: datetime | None  # first capture with league status "complete"
    reconcile_failures: int  # finalized player-weeks classed rule / unexplained
    bracket_warnings: tuple[str, ...] = ()  # e.g. a loser-advances warning from the load


@dataclass(slots=True)
class SealSeasonResult:
    year: int
    seal_id: str
    previous_seal: str
    backup_path: Path
    sealed_rows: int
    champion: str
    problems: list[str] = field(default_factory=list)


def preconditions(
    conn: sqlite3.Connection,
    year: int,
    champion: str,
    facts: SeasonCloseFacts,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Every reason not to seal ``year`` (empty = ready)."""
    now = now or datetime.now(tz=UTC)
    problems: list[str] = []
    row = conn.execute(
        "SELECT season_id, platform, status, is_sealed, champion_team_id, runner_up_team_id,"
        " last_place_team_id FROM seasons WHERE year = ?",
        (year,),
    ).fetchone()
    if row is None:
        return [f"no season {year}"]
    sid, platform, status, sealed, champ, runner, last = row
    if sealed:
        problems.append(f"{year} is already sealed")
    if platform != "sleeper":
        problems.append(f"{year} is a {platform} season; only Sleeper seasons close this way")
    if status != "completed":
        problems.append(f"{year} status is {status!r}, not 'completed'")
    if facts.complete_observed_at is None:
        problems.append("no capture has seen the league as complete")
    elif now - facts.complete_observed_at < CORRECTION_WINDOW:
        problems.append(
            "the stat-correction window is still open (league complete since "
            f"{facts.complete_observed_at:%Y-%m-%d}; wait 7 days and re-capture)"
        )
    if facts.reconcile_failures:
        problems.append(
            f"reconcile has {facts.reconcile_failures} rule/unexplained divergences; "
            "explain or fix them first"
        )
    problems.extend(f"bracket: {w}" for w in facts.bracket_warnings)
    if None in (champ, runner, last):
        problems.append("champion / runner-up / last place are not all set (bracket undecided?)")
    unplaced = conn.execute(
        "SELECT count(*) FROM teams WHERE season_id = ? AND final_rank IS NULL", (sid,)
    ).fetchone()[0]
    if unplaced:
        problems.append(f"{unplaced} teams have no final place")
    if champ is not None:
        name = conn.execute("SELECT team_name FROM teams WHERE team_id = ?", (champ,)).fetchone()
        if name is None or str(name[0]).strip().casefold() != champion.strip().casefold():
            problems.append("the champion you typed does not match the recorded champion")
    if guard_v2.trigger_problems(conn, required=True):
        problems.append("guard v2 triggers are not installed (run `ff-pipeline migrate up`)")
    return problems


def seal_season(
    conn: sqlite3.Connection,
    year: int,
    champion: str,
    facts: SeasonCloseFacts,
    *,
    backup_dir: Path,
    now: datetime | None = None,
) -> SealSeasonResult:
    require_guard_schema(conn)
    problems = preconditions(conn, year, champion, facts, now=now)
    if problems:
        raise LegacyError(f"not sealing {year}:\n  " + "\n  ".join(problems))
    report = verify(conn, full=True)
    if not report.ok:
        raise LegacyError("legacy verify is not clean; not sealing:\n" + "\n".join(report.lines()))
    previous = current_seal(conn)
    assert previous is not None  # verify passed on an adopted DB
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"fantasy-pre-seal-{year}-{stamp}.db"
    backup_database(conn, backup)

    seal_id = next_seal_id(conn)
    champ_id = conn.execute(
        "SELECT champion_team_id FROM seasons WHERE year = ?", (year,)
    ).fetchone()[0]
    created_at = now_text()
    conn.execute("BEGIN IMMEDIATE")
    try:
        set_unlock(conn, 5, f"seal season {year} ({seal_id})")
        conn.execute("UPDATE seasons SET is_sealed = 1 WHERE year = ?", (year,))
        years = sealed_years(conn)
        scans = scan_all(conn, years)
        write_baseline(
            conn,
            seal_id=seal_id,
            kind="season_close",
            created_at=created_at,
            years=years,
            scans=scans,
            git_commit=git_head(),
            extra={
                "previous_seal": previous.seal_id,
                "season_closed": year,
                "champion_team_id": champ_id,
                **guard_v2.baseline_extra(conn, years),
            },
        )
        relock(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    after = verify(conn, full=True)
    return SealSeasonResult(
        year=year,
        seal_id=seal_id,
        previous_seal=previous.seal_id,
        backup_path=backup,
        sealed_rows=sum(len(s.row_hashes) for s in scans.values()),
        champion=champion,
        problems=[] if after.ok else after.lines(),
    )


__all__ = [
    "CORRECTION_WINDOW",
    "SealSeasonResult",
    "SeasonCloseFacts",
    "preconditions",
    "seal_season",
]
