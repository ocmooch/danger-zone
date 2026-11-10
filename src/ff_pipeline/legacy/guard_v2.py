"""Guard v2 (S9-D6): Sleeper-era coverage on top of the frozen seal-v1 guard.

* **Triggers** (``triggers.trigger_ddl_v2``, migration ``s9_legacy_guard_v2``):
  ``verify`` checks the ones present are unaltered, and requires all of them
  once a baseline says ``guard_version >= 2``.
* **Fingerprints**: a baseline written by ``seal-season`` (or an amendment after
  it) stores ``platform_tables``, the scoped fingerprints of ``GUARD_V2_TABLES``
  for the sealed years. ``verify`` compares them; ``amend`` re-seals when they
  change and carries them forward.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ff_pipeline.legacy.fingerprint import scan_table
from ff_pipeline.legacy.spec import GUARD_V2_TABLES
from ff_pipeline.legacy.triggers import trigger_ddl_v2

if TYPE_CHECKING:
    import sqlite3

GUARD_VERSION = 2


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def scan_v2(conn: sqlite3.Connection, years: list[int]) -> dict[str, dict[str, Any]]:
    """``{table: fingerprint}`` of the v2-guarded columns for ``years``."""
    return {
        spec.name: scan_table(conn, spec, years).fingerprint
        for spec in GUARD_V2_TABLES
        if has_table(conn, spec.name)
    }


def baseline_extra(conn: sqlite3.Connection, years: list[int]) -> dict[str, Any]:
    return {"guard_version": GUARD_VERSION, "platform_tables": scan_v2(conn, years)}


def is_v2(manifest: dict[str, Any]) -> bool:
    return int(manifest.get("guard_version") or 1) >= GUARD_VERSION


def trigger_problems(conn: sqlite3.Connection, *, required: bool) -> list[str]:
    live = dict(
        conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
    )
    ddl = trigger_ddl_v2()
    if not required and not any(name in live for name, _ in ddl):
        return []  # migration not applied yet, and no v2 baseline needs it
    problems: list[str] = []
    for name, sql in ddl:
        if name not in live:
            problems.append(f"guard v2 trigger {name} is missing")
        elif live[name] != sql:
            problems.append(f"guard v2 trigger {name} has been altered")
    return problems


def fingerprint_problems(
    conn: sqlite3.Connection, manifest: dict[str, Any], years: list[int], *, full: bool
) -> list[str]:
    if not is_v2(manifest):
        return []
    want: dict[str, Any] = manifest.get("platform_tables") or {}
    got = scan_v2(conn, years)
    problems: list[str] = []
    for name in sorted(set(want) | set(got)):
        w, g = want.get(name), got.get(name)
        if w is None or g is None:
            problems.append(f"v2 {name}: present in {'live' if w is None else 'seal'} only")
            continue
        differs = g["row_count"] != w["row_count"] or g["table_sha256"] != w["table_sha256"]
        if full:
            differs = differs or g.get("by_season") != w.get("by_season")
        if differs:
            seasons = sorted(
                y
                for y in set(g.get("by_season", {})) | set(w.get("by_season", {}))
                if g.get("by_season", {}).get(y) != w.get("by_season", {}).get(y)
            )
            problems.append(
                f"v2 {name}: rows {g['row_count']} vs seal {w['row_count']}, "
                f"Sleeper-era columns differ (seasons: {', '.join(seasons) or '-'})"
            )
    return problems


def changed_tables(
    conn: sqlite3.Connection, manifest: dict[str, Any], years: list[int]
) -> list[str]:
    """v2 tables whose fingerprint moved since ``manifest`` (for ``amend``)."""
    if not is_v2(manifest):
        return []
    want: dict[str, Any] = manifest.get("platform_tables") or {}
    got = scan_v2(conn, years)
    return sorted(n for n in set(want) | set(got) if want.get(n) != got.get(n))


__all__ = [
    "GUARD_VERSION",
    "baseline_extra",
    "changed_tables",
    "fingerprint_problems",
    "is_v2",
    "scan_v2",
    "trigger_problems",
]
