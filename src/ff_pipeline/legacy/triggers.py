"""DDL for the legacy guard triggers (SQLite).

The ``s1_legacy_guard`` migration holds a literal copy of this output (migrations
stay frozen); a unit test asserts the two are identical.

Every guard is bypassed while ``legacy_guard.unlocked_until`` (UTC,
``YYYY-MM-DDTHH:MM:SSZ``) is in the future — ``legacy amend`` opens that window.

UPDATE guards only fire when a guarded column actually changes value, so an
idempotent re-write of identical data (an upsert re-run, the Hamlin override)
passes, while any real change is refused. Season guards are inert until a
season has ``is_sealed = 1``; registered-row guards are inert until rows are
listed in ``legacy_sealed_rows``; so a fresh or test DB behaves as before.
"""

from __future__ import annotations

from ff_pipeline.legacy.spec import (
    GUARDED_TABLES,
    SEASON_SEAL_COLUMNS,
    GuardedTable,
    GuardKind,
)

LOCKED = (
    "COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '')"
    " < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
)
AMEND_HINT = "use ff-pipeline legacy amend"


def _abort(message: str) -> str:
    return f"SELECT RAISE(ABORT, 'legacy_guard: {message}');"


def _changed(columns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns) + ")"


def _sealed(spec: GuardedTable, row: str) -> str:
    """SQL predicate: ``row`` (NEW/OLD) belongs to a sealed season."""
    by_year = (
        f"EXISTS (SELECT 1 FROM seasons s WHERE s.year = {row}.season_year AND s.is_sealed = 1)"
    )
    if spec.season_link == "season_id":
        return f"(SELECT is_sealed FROM seasons WHERE season_id = {row}.season_id) = 1"
    if spec.season_link == "season_year":
        return by_year
    if spec.season_link == "team_id":
        by_team = (
            "(SELECT s.is_sealed FROM teams tm JOIN seasons s ON s.season_id = tm.season_id"
            f" WHERE tm.team_id = {row}.team_id) = 1"
        )
        return f"({by_team} OR {by_year})"
    raise ValueError(f"{spec.name}: unknown season link {spec.season_link!r}")


def _registered(spec: GuardedTable, row: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM legacy_sealed_rows r"
        f" WHERE r.table_name = '{spec.name}' AND r.pk = CAST({row}.{spec.pk} AS TEXT))"
    )


def _trigger(name: str, event: str, table: str, when: str, body: str) -> tuple[str, str]:
    sql = f"CREATE TRIGGER {name} BEFORE {event} ON {table}\nWHEN {when}\nBEGIN {body} END"
    return name, sql


def _table_triggers(spec: GuardedTable) -> list[tuple[str, str]]:
    t = spec.name
    prefix = f"legacy_guard_{t}"
    if spec.kind is GuardKind.SEASON:
        msg = f"{t} row belongs to a sealed season; {AMEND_HINT}"
        return [
            _trigger(
                f"{prefix}_insert",
                "INSERT",
                t,
                f"{LOCKED} AND {_sealed(spec, 'NEW')}",
                _abort(msg),
            ),
            _trigger(
                f"{prefix}_update",
                "UPDATE",
                t,
                f"{LOCKED} AND ({_sealed(spec, 'OLD')} OR {_sealed(spec, 'NEW')})"
                f" AND {_changed(spec.legacy_columns)}",
                _abort(msg),
            ),
            _trigger(
                f"{prefix}_delete",
                "DELETE",
                t,
                f"{LOCKED} AND {_sealed(spec, 'OLD')}",
                _abort(msg),
            ),
        ]
    if spec.kind is GuardKind.SEASON_ROW:
        msg = f"season is sealed; {AMEND_HINT}"
        return [
            _trigger(
                f"{prefix}_update",
                "UPDATE",
                t,
                f"{LOCKED} AND OLD.is_sealed = 1"
                f" AND {_changed(spec.legacy_columns + SEASON_SEAL_COLUMNS)}",
                _abort(msg),
            ),
            _trigger(
                f"{prefix}_delete",
                "DELETE",
                t,
                f"{LOCKED} AND OLD.is_sealed = 1",
                _abort(msg),
            ),
        ]
    msg = f"{t} row is sealed; {AMEND_HINT}"
    return [
        _trigger(
            f"{prefix}_update",
            "UPDATE",
            t,
            f"{LOCKED} AND {_registered(spec, 'OLD')} AND {_changed(spec.legacy_columns)}",
            _abort(msg),
        ),
        _trigger(
            f"{prefix}_delete",
            "DELETE",
            t,
            f"{LOCKED} AND {_registered(spec, 'OLD')}",
            _abort(msg),
        ),
    ]


def _bookkeeping_triggers() -> list[tuple[str, str]]:
    """Protect the guard's own audit tables."""
    out: list[tuple[str, str]] = []
    for table in ("legacy_amendments", "legacy_seals"):
        for event in ("UPDATE", "DELETE"):
            out.append(
                _trigger(
                    f"legacy_guard_{table}_{event.lower()}",
                    event,
                    table,
                    "1",
                    _abort(f"{table} is append-only"),
                )
            )
    for event in ("INSERT", "UPDATE", "DELETE"):
        out.append(
            _trigger(
                f"legacy_guard_legacy_sealed_rows_{event.lower()}",
                event,
                "legacy_sealed_rows",
                LOCKED,
                _abort(f"legacy_sealed_rows changes only inside an unlock; {AMEND_HINT}"),
            )
        )
    return out


def trigger_ddl() -> list[tuple[str, str]]:
    """(name, CREATE TRIGGER sql) for every guard trigger, in a stable order."""
    out: list[tuple[str, str]] = []
    for spec in GUARDED_TABLES:
        out.extend(_table_triggers(spec))
    out.extend(_bookkeeping_triggers())
    return out


def trigger_names() -> list[str]:
    return [name for name, _ in trigger_ddl()]
