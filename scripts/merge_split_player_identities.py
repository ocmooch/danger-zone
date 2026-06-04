"""One-off repair: merge split NFL.com / nflverse player identities.

Background
----------
Two ingest quirks split a player into a stats-bearing nflverse row and
a statless NFL.com stub (``nfl_com_player_id`` set, zero
``player_stats_raw``). ``verify --sweep`` matches gamecenter starters by
``nfl_com_player_id`` and reads stats off that row, so a split shows up
as ``our_raw_stats_missing``.

1. **Bogus position.** The NFL.com roster parser used to capture UI
   strings ("Season is Over - Add to Watch List") and flex slot labels
   ("R/W/T") as a player's ``position``, defeating the resolver's
   name+position fuzzy match. The parser is now fixed (``_clean_position``
   whitelist) so *new* ingests won't split this way.
2. **Abbreviated names.** The gamecenter lineup pages render
   ``"E. Pineiro"`` where nflverse has ``"Eddy Pineiro"``. The
   abbreviated first name defeats both the exact and normalized
   name match, so the history reconstruction (which scrapes gamecenter)
   minted a fresh stub for ~600 players. Folding the stub stamps its
   ``nfl_com_player_id`` onto the canonical row, so subsequent
   reconstructions match by ID and won't re-split.

This script repairs the rows already in the DB.

Strategy
--------
For each "stub" (``nfl_com_player_id`` set, zero raw-stat rows) find the
canonical stats-bearing row for the same player and fold the stub into
it: repoint every ``player_id`` foreign key, delete the stub, then stamp
the freed ``nfl_com_player_id`` (and any other external IDs) onto the
canonical row.

Matching is deliberately conservative:

* **Exact ``name_full``** (case-insensitive) is tried before a
  normalized match, so "Frank Gore Jr." folds into the son, not his
  Hall-of-Fame father "Frank Gore".
* Only if both name lookups miss *and* the stub name is abbreviated
  ("X. Lastname") do we fall back to a **(first-initial, last-token)**
  match against stats-bearing rows. This reaches the gamecenter
  abbreviations but is funnelled through the same guards below, so a
  common "D. Johnson" with several real RBs stays skipped.
* When the stub still carries a *valid* position, candidates are
  filtered to it.
* If several stats-bearing rows share the name (or initial+last), one is
  chosen only when it **dominates** by raw-row count (>=10 rows and
  either the runner-up has none or the leader has >=5x as many).
  Otherwise the stub is skipped and reported — e.g. multiple real "Mike
  Williams" WRs.

Team defenses ("Houston Texans") and players nflverse never recorded
have no stats-bearing match and are left untouched.

Usage
-----
    uv run python scripts/merge_split_player_identities.py            # dry run
    uv run python scripts/merge_split_player_identities.py --apply    # commit

``--apply`` snapshots the SQLite file to ``data/backups/`` first.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.parsers import _clean_position
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.maintenance import recompute_rostered_spans
from ff_pipeline.settings import get_settings

# External-ID columns we move from stub → canonical (NULL → value only).
_EXTERNAL_ID_COLS = ("nfl_com_player_id", "gsis_id", "sleeper_id", "espn_id", "yahoo_id")

# Leader must clear this many raw rows AND beat the runner-up by this
# factor before we accept it for a same-name multi-candidate stub.
_MIN_LEADER_ROWS = 10
_DOMINANCE_FACTOR = 5


# Generational suffixes stripped during normalization. "v"/"i"/"x" double
# as single-letter initials, so they are only dropped when they are *not*
# the leading token (see _normalize) — a leading "v" is "V. Cruz", not a fifth.
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _normalize(name: str | None) -> str:
    if not name:
        return ""
    decoded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    decoded = decoded.lower()
    # Strip punctuation without inserting space so "D.J." collapses to "dj"
    # (keeps "D.J. Moore" ≡ "DJ Moore"); spaces in the source still split.
    decoded = re.sub(r"[^a-z ]", "", decoded)
    # Drop generational suffixes, but never the leading token: a lone
    # "v"/"i"/"x" there is an abbreviated first name ("V. Cruz" → "v cruz"),
    # not a roman-numeral suffix. Eating it collapsed the name to a single
    # token and silently defeated the initial+last match path.
    tokens = [t for i, t in enumerate(decoded.split()) if not (i > 0 and t in _NAME_SUFFIXES)]
    return " ".join(tokens)


class _Player:
    __slots__ = ("ids", "name_full", "nfl_com_player_id", "player_id", "position", "raw_rows")

    def __init__(self, pid, name, pos, nflc, raw_rows, ids):
        self.player_id = pid
        self.name_full = name
        self.position = pos
        self.nfl_com_player_id = nflc
        self.raw_rows = raw_rows
        self.ids = ids  # dict of external-id col -> value


def _load_players(conn) -> tuple[list[_Player], dict[int, _Player]]:
    raw_counts: dict[int, int] = dict(
        conn.execute(
            text("SELECT player_id, COUNT(*) FROM player_stats_raw GROUP BY player_id")
        ).all()
    )
    cols = ", ".join(("player_id", "name_full", "position", *_EXTERNAL_ID_COLS))
    rows = conn.execute(text(f"SELECT {cols} FROM players")).mappings().all()
    players: list[_Player] = []
    by_id: dict[int, _Player] = {}
    for r in rows:
        ids = {c: r[c] for c in _EXTERNAL_ID_COLS}
        p = _Player(
            r["player_id"],
            r["name_full"],
            r["position"],
            r["nfl_com_player_id"],
            raw_counts.get(r["player_id"], 0),
            ids,
        )
        players.append(p)
        by_id[p.player_id] = p
    return players, by_id


def _pick_canonical(
    stub: _Player, candidates: list[_Player], *, prune_id_conflicts: bool = True
) -> tuple[_Player | None, str]:
    """Choose the stats-bearing row to fold ``stub`` into, or explain why not.

    ``prune_id_conflicts`` drops candidates that already carry a *different*
    ``nfl_com_player_id`` before counting. That is right for the name-keyed
    paths (a different id means a different player). For the abbreviated
    initial+last path it must stay ``False``: dropping a same-initial-last
    sibling there would turn a genuinely ambiguous "J. Nelson" (J.J. vs
    Jordy) into a false "unique" and fold the stub into the wrong player.
    """
    if prune_id_conflicts:
        candidates = [
            c
            for c in candidates
            if not c.nfl_com_player_id or c.nfl_com_player_id == stub.nfl_com_player_id
        ]
    if not candidates:
        return None, "no_stats_bearing_match"

    # If the stub still has a real position, prefer same-position rows.
    stub_pos = _clean_position(stub.position)
    if stub_pos is not None:
        same_pos = [c for c in candidates if (c.position or "").upper() == stub_pos]
        if same_pos:
            candidates = same_pos

    if len(candidates) == 1:
        return candidates[0], "unique"

    ranked = sorted(candidates, key=lambda c: c.raw_rows, reverse=True)
    leader, runner_up = ranked[0], ranked[1]
    dominates = leader.raw_rows >= _MIN_LEADER_ROWS and (
        runner_up.raw_rows == 0 or leader.raw_rows >= _DOMINANCE_FACTOR * runner_up.raw_rows
    )
    if dominates:
        return leader, "dominant"
    return None, "ambiguous"


def _fk_tables(conn) -> list[str]:
    """Every table (besides ``players``) with a ``player_id`` column."""
    names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
    out = []
    for t in names:
        if t == "players":
            continue
        cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({t})"))]
        if "player_id" in cols:
            out.append(t)
    return out


def _apply_merge(conn, stub: _Player, canonical: _Player, fk_tables: list[str]) -> None:
    for t in fk_tables:
        conn.execute(
            text(f"UPDATE {t} SET player_id = :dst WHERE player_id = :src"),
            {"dst": canonical.player_id, "src": stub.player_id},
        )
    conn.execute(text("DELETE FROM players WHERE player_id = :pid"), {"pid": stub.player_id})
    # Stamp the freed external IDs onto the canonical row (NULL → value).
    fills = {
        col: stub.ids[col] for col in _EXTERNAL_ID_COLS if stub.ids[col] and not canonical.ids[col]
    }
    if fills:
        assignments = ", ".join(f"{col} = :{col}" for col in fills)
        conn.execute(
            text(f"UPDATE players SET {assignments} WHERE player_id = :pid"),
            {**fills, "pid": canonical.player_id},
        )
        for col, val in fills.items():
            canonical.ids[col] = val


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit the merges (default: dry run).")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    ap.add_argument("--show", type=int, default=15, help="How many example skips to print.")
    args = ap.parse_args(argv)

    db_url = args.database_url or get_settings().database_url

    if args.apply and db_url.startswith("sqlite:///"):
        src = Path(db_url.removeprefix("sqlite:///"))
        if src.exists():
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            dst = src.parent / "backups" / f"{src.stem}-premerge-{stamp}.db"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"Backed up DB → {dst}")

    engine = create_app_engine(db_url)
    planned: list[tuple[_Player, _Player, str]] = []
    skips: dict[str, list[_Player]] = defaultdict(list)
    try:
        with engine.connect() as conn:
            players, _ = _load_players(conn)
            fk_tables = _fk_tables(conn)

            stats_bearing = [p for p in players if p.raw_rows > 0]
            by_exact: dict[str, list[_Player]] = defaultdict(list)
            by_norm: dict[str, list[_Player]] = defaultdict(list)
            # (first-initial, last-token) → candidates, for abbreviated
            # gamecenter names ("E. Pineiro") that the exact/normalized
            # lookups can't reach. Position + dominance guards in
            # _pick_canonical keep this conservative.
            by_initial_last: dict[tuple[str, str], list[_Player]] = defaultdict(list)
            for p in stats_bearing:
                by_exact[(p.name_full or "").lower()].append(p)
                by_norm[_normalize(p.name_full)].append(p)
                toks = _normalize(p.name_full).split()
                if len(toks) >= 2:
                    by_initial_last[(toks[0][0], toks[-1])].append(p)

            stubs = [p for p in players if p.nfl_com_player_id and p.raw_rows == 0]
            for stub in stubs:
                via_initial = False
                cands = by_exact.get((stub.name_full or "").lower())
                if not cands:
                    cands = by_norm.get(_normalize(stub.name_full), [])
                if not cands:
                    # Abbreviated "X. Lastname" → match by initial+last.
                    toks = _normalize(stub.name_full).split()
                    if len(toks) >= 2 and len(toks[0]) == 1:
                        cands = by_initial_last.get((toks[0][0], toks[-1]), [])
                        via_initial = bool(cands)
                canonical, reason = _pick_canonical(
                    stub, list(cands), prune_id_conflicts=not via_initial
                )
                if canonical is None:
                    skips[reason].append(stub)
                    continue
                if via_initial:
                    reason = f"initial+{reason}"
                planned.append((stub, canonical, reason))

            print(f"FK tables repointed per merge: {fk_tables}")
            print(f"\nStubs (nfl_com id, no raw stats): {len(stubs)}")
            print(f"  mergeable: {len(planned)}")
            by_reason: dict[str, int] = defaultdict(int)
            for _, _, reason in planned:
                by_reason[reason] += 1
            for reason, n in sorted(by_reason.items()):
                print(f"    via {reason}: {n}")
            for reason, items in sorted(skips.items()):
                print(f"  skipped [{reason}]: {len(items)}")

            if skips.get("ambiguous"):
                print(f"\nAmbiguous skips (first {args.show}):")
                for s in skips["ambiguous"][: args.show]:
                    print(
                        f"    {s.name_full!r} (pos={s.position!r}, nfl_com={s.nfl_com_player_id})"
                    )

            if not args.apply:
                print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
                return 0

            applied = 0
            integrity_failures = 0
            # The SELECTs above auto-began a read transaction; close it so we
            # can open an explicit write transaction with nested savepoints.
            conn.rollback()
            with conn.begin():
                for stub, canonical, _reason in planned:
                    sp = conn.begin_nested()
                    try:
                        _apply_merge(conn, stub, canonical, fk_tables)
                        sp.commit()
                        applied += 1
                    except IntegrityError as exc:
                        sp.rollback()
                        integrity_failures += 1
                        print(
                            f"  INTEGRITY skip: {stub.name_full!r} "
                            f"(stub {stub.player_id} → {canonical.player_id}): "
                            f"{str(exc.orig)[:120]}"
                        )
            print(f"\nApplied {applied} merges ({integrity_failures} integrity skips).")

            # Folding a stub repoints its team_rosters rows onto the canonical
            # player, which can widen that player's league-relevance span. Refresh
            # the materialized first/last_rostered_season so the players index
            # stays correct without waiting for the next NFL.com sync.
            if applied:
                with Session(engine) as ss:
                    touched = recompute_rostered_spans(ss)
                    ss.commit()
                print(f"Recomputed rostered-season spans ({touched} player rows).")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
