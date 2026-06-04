"""Audit: find players whose NFL.com roster seasons are temporally
inconsistent with their nflverse stats — the fingerprint of a misstamped
``nfl_com_player_id``.

Why
---
``team_rosters`` rows resolve to a ``players`` row via its
``nfl_com_player_id``; ``player_stats_raw`` rows attach via its ``gsis_id``.
When the resolver folds an abbreviated NFL.com lineup name ("S. Smith") onto
the wrong same-name canonical, that row ends up holding *another* player's
NFL.com id and roster history while keeping its own gsis stats. The tell is a
roster season that the player's nflverse career can't explain — e.g. rostered
in 2010 but first stat (and ``rookie_year``) in 2021. The real player is left
stranded: a same-name row with stats but no ``nfl_com_player_id`` and no
roster rows. See ``scripts/untangle_nelson_conflation.py`` (the first instance)
and ``scripts/untangle_misstamped_roster_identities.py`` (the batch repair).

Signals (skill positions only; DST/team rows are a separate path)
-----------------------------------------------------------------
* **A — rostered before debut**: ``first_rostered_season < rookie_year``.
  Impossible; the strongest signal.
* **B — disjoint eras**: the set of rostered seasons and the set of
  stat seasons do not intersect at all.
* **C — rostered long after career end**: ``last_rostered_season >
  last_season + 2``. Catches the partial-overlap variant that A/B miss,
  where a younger namesake's rows extend an older player's roster span past
  his real career (e.g. Michael Crabtree's WR weeks on Tom Crabtree's TE
  row). Genuinely long-retired players kept on a keeper roster also surface
  here and are cleared in manual review (no younger same-name owner exists).

A single rostered season with stats in the immediately adjacent years is
*not* flagged — that is the benign "rostered but recorded no stats" case (a
player on a roster through an injury/redshirt year, e.g. Reserve/COVID 2020).

This is read-only: it reports, it never mutates. Re-run after every
reconstruction to catch regressions (the resolver era-guard added in
``normalizer/player_ids.py`` should keep this empty going forward).

Usage
-----
    uv run python scripts/audit_roster_stat_era_mismatch.py
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.settings import get_settings

_SKILL = ("QB", "RB", "WR", "TE", "K")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL from settings.")
    args = ap.parse_args(argv)
    db_url = args.database_url or get_settings().database_url

    engine = create_app_engine(db_url)
    flagged: list[dict] = []
    try:
        with engine.connect() as conn:
            placeholders = ", ".join(f"'{p}'" for p in _SKILL)
            players = (
                conn.execute(
                    text(
                        f"""
                    SELECT p.player_id, p.name_full, p.position, p.rookie_year,
                           p.last_season, p.nfl_com_player_id,
                           p.first_rostered_season AS fr, p.last_rostered_season AS lr
                    FROM players p
                    WHERE p.position IN ({placeholders})
                      AND EXISTS (SELECT 1 FROM team_rosters r WHERE r.player_id = p.player_id)
                      AND EXISTS (SELECT 1 FROM player_stats_raw s WHERE s.player_id = p.player_id)
                    """
                    )
                )
                .mappings()
                .all()
            )

            for p in players:
                pid = p["player_id"]
                rseasons = {
                    r[0]
                    for r in conn.execute(
                        text("SELECT DISTINCT season_year FROM team_rosters WHERE player_id = :p"),
                        {"p": pid},
                    )
                }
                sseasons = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT DISTINCT season_year FROM player_stats_raw WHERE player_id = :p"
                        ),
                        {"p": pid},
                    )
                }
                if not rseasons or not sseasons:
                    continue

                signal_a = p["rookie_year"] is not None and min(rseasons) < p["rookie_year"]
                disjoint = not (rseasons & sseasons)
                signal_c = p["last_season"] is not None and max(rseasons) > p["last_season"] + 2
                # Benign single-season "rostered but no stats that year" (injury/
                # redshirt): one rostered season sitting adjacent to a stat season.
                benign = (
                    disjoint
                    and len(rseasons) == 1
                    and any(abs(next(iter(rseasons)) - s) <= 1 for s in sseasons)
                )
                if (signal_a or disjoint or signal_c) and not benign:
                    signal = (
                        "A:before-debut"
                        if signal_a
                        else "B:disjoint-era"
                        if disjoint
                        else "C:after-career"
                    )
                    flagged.append(
                        {
                            **dict(p),
                            "rostered": f"{min(rseasons)}-{max(rseasons)}",
                            "stats": f"{min(sseasons)}-{max(sseasons)}",
                            "signal": signal,
                        }
                    )
    finally:
        engine.dispose()

    print("Skill players with both roster + stat data audited.")
    print(f"Temporal-mismatch suspects: {len(flagged)}\n")
    for f in sorted(flagged, key=lambda x: (x["signal"], x["name_full"])):
        print(
            f"  [{f['signal']:<14}] pid={f['player_id']:<6} {f['name_full']:<22}"
            f"{f['position']:<3} rookie={f['rookie_year']!s:<6} "
            f"rostered={f['rostered']:<12} stats={f['stats']:<12} nflcom={f['nfl_com_player_id']}"
        )
    if flagged:
        print(
            "\nEach suspect: confirm the misplaced nfl_com_player_id's true owner "
            "(a stranded same-name row), then repair via "
            "scripts/untangle_misstamped_roster_identities.py."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
