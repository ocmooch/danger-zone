# scripts/archive — applied one-off repairs

These are **spent, non-reusable** maintenance scripts. Each fixed a specific
data defect in `data/fantasy.db`, was run once against the live DB, and its
result is already committed to the database (and merged to `main`). They are
kept here for provenance — to document *how* a given correction was made — not
to be re-run. Re-running them against the current DB is at best a no-op and at
worst harmful, since the conditions they targeted no longer exist.

For the reasoning behind each repair, see the script docstring and the matching
entry in `docs/10_OPEN_QUESTIONS.md` / `docs/10_OPEN_QUESTIONS_ARCHIVE.md`.

These scripts import each other as siblings (e.g. `untangle_nelson_conflation`
reuses helpers from `merge_split_player_identities`), so they must stay
together in this directory.

| Script | Applied | What it repaired |
| --- | --- | --- |
| `merge_split_player_identities.py` | 2026-05-29 | Merged players split into a stats-bearing nflverse row and a statless NFL.com stub (the `our_raw_stats_missing` class in `verify --sweep`). |
| `merge_roster_name_stubs.py` | 2026-06-03 | Folded abbreviated NFL.com roster name-stubs ("V. Cruz") onto their canonical nflverse players. |
| `audit_roster_stat_era_mismatch.py` | 2026-06-04 | Audit (read-only): flagged roster rows whose NFL.com seasons were temporally inconsistent with their nflverse stats — the misstamped-`nfl_com_player_id` fingerprint. |
| `untangle_nelson_conflation.py` | 2026-06-04 | Untangled the J.J. / Jordy Nelson identity conflation that `merge_roster_name_stubs` deliberately held back. |
| `untangle_misstamped_roster_identities.py` | 2026-06-04 | Re-homed roster identities fuzzy-matched onto the wrong same-name player (the Nelson defect, generalized). |
| `merge_owner_identities.py` | 2026-06-07 | Merged duplicate owner rows into canonical manager identities (the two Dans / Adams / Ills splits). |
| `repair_owner_identity_and_phantom_teams.py` | 2026-06-09 | Repaired owner-identity splits and merged phantom franchise-duplicate team rows left by an early year-less backfill. |
