"""Move the NFL.com 2026 placeholder out of season 2026 (D-2; S4-D1, S4-D2).

The 12 ``teams`` + their ``team_rosters`` rows that NFL.com produced for 2026
before the league moved to Sleeper are copied verbatim into
``legacy_nfl_com_2026_preseason`` (one JSON row each, original primary key
kept), checked column by column, and only then removed from the live tables.
Season 2026 keeps its ``season_id`` and is re-labelled ``platform='sleeper'``.

``legacy amend`` cannot audit this: it records only changes to *sealed* rows,
and 2026 was never sealed. So this module writes its own ``legacy_amendments``
row in the same transaction. Sealed data does not change, so there is no new
seal; the stub also stays in the seal-v1 vault archive.

Rostered-season spans (``players.last_rostered_season``) must be recomputed
afterwards; the CLI does that through ``repository.maintenance``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ff_pipeline.legacy.seal import LegacyError, git_head, now_text
from ff_pipeline.legacy.verify import verify

if TYPE_CHECKING:
    import sqlite3

STUB_YEAR = 2026
QUARANTINE = "legacy_nfl_com_2026_preseason"
DEFAULT_REASON = "Archive 2026 NFL.com placeholder season (D-2): Sleeper owns season 2026"


@dataclass(slots=True)
class QuarantineResult:
    already_done: bool = False
    amendment_id: int | None = None
    season_id: int | None = None
    team_ids: list[int] = field(default_factory=list)
    roster_rows: int = 0
    seal_id: str | None = None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def quarantine_2026_stub(
    conn: sqlite3.Connection,
    *,
    sleeper_league_id: str,
    reason: str = DEFAULT_REASON,
    command: str = "ff-pipeline legacy quarantine-2026-stub",
    fail_check: bool = False,  # test hook: simulate a copy mismatch
) -> QuarantineResult:
    """Quarantine the stub in one transaction. Raises ``LegacyError`` on any refusal."""
    result = QuarantineResult()
    report = verify(conn, full=True)
    if not report.adopted or not report.ok:
        raise LegacyError("Legacy verify is not clean; refusing:\n" + "\n".join(report.lines()))
    result.seal_id = report.seal_id

    season = _rows(
        conn,
        "SELECT season_id, is_sealed, platform FROM seasons WHERE year = ?",
        (STUB_YEAR,),
    )
    if len(season) != 1:
        raise LegacyError(f"expected one season {STUB_YEAR}, found {len(season)}")
    s = season[0]
    result.season_id = int(s["season_id"])
    moved = conn.execute(f"SELECT count(*) FROM {QUARANTINE}").fetchone()[0]
    if s["platform"] == "sleeper" and moved:
        result.already_done = True
        return result
    if s["is_sealed"]:
        raise LegacyError(f"season {STUB_YEAR} is sealed; refusing")
    if s["platform"] != "nfl_com":
        raise LegacyError(f"season {STUB_YEAR} platform is {s['platform']!r}, expected 'nfl_com'")
    if moved:
        raise LegacyError(f"{QUARANTINE} already has {moved} rows; refusing to mix moves")

    conn.execute("BEGIN IMMEDIATE")
    try:
        teams = _rows(
            conn, "SELECT * FROM teams WHERE season_id = ? ORDER BY team_id", (result.season_id,)
        )
        team_ids = [int(t["team_id"]) for t in teams]
        marks = ",".join("?" * len(team_ids))
        rosters = (
            _rows(
                conn,
                f"SELECT * FROM team_rosters WHERE team_id IN ({marks}) ORDER BY roster_id",
                tuple(team_ids),
            )
            if team_ids
            else []
        )
        _refuse_other_references(conn, team_ids)

        cur = conn.execute(
            "INSERT INTO legacy_amendments (created_at, reason, command, git_commit,"
            " before_seal, after_seal, tables_changed, rows_changed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_text(),
                reason,
                command,
                git_head(),
                result.seal_id,
                result.seal_id,  # sealed data unchanged: no new seal (S4-D1)
                json.dumps({"teams": [STUB_YEAR], "team_rosters": [STUB_YEAR]}),
                json.dumps(
                    {
                        "teams": {"moved": team_ids},
                        "team_rosters": {
                            "moved": [int(r["roster_id"]) for r in rosters],
                            "count": len(rosters),
                        },
                        "seasons": {"relabelled": result.season_id, "platform": "sleeper"},
                        "destination": QUARANTINE,
                    }
                ),
            ),
        )
        amendment_id = int(cur.lastrowid or 0)
        stamp = now_text()
        originals: dict[tuple[str, str], dict[str, Any]] = {}
        for table, rows, pk in (
            ("teams", teams, "team_id"),
            ("team_rosters", rosters, "roster_id"),
        ):
            for row in rows:
                originals[(table, str(row[pk]))] = row
                conn.execute(
                    f"INSERT INTO {QUARANTINE} (source_table, original_pk, row_data,"
                    " quarantined_at, amendment_id) VALUES (?, ?, ?, ?, ?)",
                    (table, str(row[pk]), json.dumps(row), stamp, amendment_id),
                )

        # Byte-for-byte check before anything is deleted.
        for table, pk, data in conn.execute(
            f"SELECT source_table, original_pk, row_data FROM {QUARANTINE}"
        ):
            copy = json.loads(data)
            if fail_check or copy != originals.get((table, pk)):
                raise LegacyError(f"quarantine copy of {table} {pk} does not match; rolled back")
        if len(originals) != len(teams) + len(rosters):
            raise LegacyError("quarantine row count mismatch; rolled back")

        if team_ids:
            conn.execute(f"DELETE FROM team_rosters WHERE team_id IN ({marks})", tuple(team_ids))
            conn.execute(f"DELETE FROM teams WHERE team_id IN ({marks})", tuple(team_ids))
        conn.execute(
            "UPDATE seasons SET platform = 'sleeper', platform_league_id = ?,"
            " updated_at = CURRENT_TIMESTAMP WHERE season_id = ?",
            (sleeper_league_id, result.season_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    result.amendment_id = amendment_id
    result.team_ids = team_ids
    result.roster_rows = len(rosters)
    after = verify(conn, full=True)
    if not after.ok:
        raise LegacyError("legacy verify failed AFTER the move:\n" + "\n".join(after.lines()))
    return result


def _refuse_other_references(conn: sqlite3.Connection, team_ids: list[int]) -> None:
    """The stub must be self-contained: nothing but its rosters may point at its teams."""
    if not team_ids:
        return
    marks = ",".join("?" * len(team_ids))
    checks = [
        ("matchups", "team_id"),
        ("matchups", "opponent_team_id"),
        ("transactions", "team_id"),
        ("transactions", "counterpart_team_id"),
        ("seasons", "champion_team_id"),
        ("seasons", "runner_up_team_id"),
        ("seasons", "last_place_team_id"),
        ("playoff_brackets", "team1_id"),
        ("playoff_brackets", "team2_id"),
    ]
    for table, col in checks:
        n = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {col} IN ({marks})", tuple(team_ids)
        ).fetchone()[0]
        if n:
            raise LegacyError(f"{n} {table}.{col} rows reference stub teams; refusing")


__all__ = ["DEFAULT_REASON", "QuarantineResult", "quarantine_2026_stub"]
