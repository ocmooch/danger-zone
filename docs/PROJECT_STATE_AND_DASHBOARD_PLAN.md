# Project State and Dashboard Integration Plan

Status as of 2026-06-07. This document exists because Phase 1 is functionally
complete, but the live database still has data-quality cleanup work that should
be resolved before `dz-dashboard` treats historical team totals and owner
aggregates as final.

## Current State

- Roadmap milestones M0-M11 are complete in `docs/09_ROADMAP.md`.
- The active branch is `feature/up-review-cleanup`.
- Focused validation on the active branch passes:
  - `uv run pytest tests/integration/test_nfl_com_history.py tests/unit/test_nfl_com_parsers.py tests/unit/test_cli.py -x`
  - `58 passed`
- Full validation passes:
  - `uv run pytest`
  - `473 passed`
- Static checks pass:
  - `uv run ruff check src tests scripts/merge_owner_identities.py`
  - `uv run ruff format --check src tests scripts/merge_owner_identities.py`
  - `uv run mypy src`

## Work Completed In This Pass

The current-season NFL.com team upsert now uses NFL.com's stable numeric team id
when it is available. Previously, `_upsert_teams` only conflicted on
`(season_id, team_name)`, so a team rename could create a duplicate internal
`teams` row for the same NFL.com team id. The patched path updates the existing
row matched by `team_abbrev` before falling back to the legacy team-name upsert.

Regression coverage added:

- `test_current_owner_upsert_uses_nfl_team_id_across_renames`

This prevents a repeat of the duplicate-team shape found in the live DB. It does
not mutate existing live data.

## Blocking Data-Quality Issue

`verify --reconcile` is still the trust gate for historical team totals.
Current live-DB check:

```bash
uv run ff-pipeline verify --season 2024 --week 1 --reconcile
```

Result:

- 13 team rows compared
- 4 passed
- 9 failed
- one explicit `no_starters_recorded` artifact

The `no_starters_recorded` artifact is explained by duplicate 2024 team rows for
NFL.com team id `7`:

| team_id | team_name | nfl_com_team_id | matchups | roster_rows |
|---:|---|---:|---:|---:|
| 192 | 1000 Bottles of Baby Boyle | 7 | 16 | 245 |
| 222 | Rev Russell's Sunday Service | 7 | 1 | 15 |

Week 1 has a matchup row for both ids, but only `222` has week-1 starters. This
is stale live data, not an API-contract problem.

The duplicate-team issue is broader than 2024. Live DB audit:

```sql
select s.year, t.team_abbrev, count(*) c
from teams t
join seasons s on s.season_id = t.season_id
where t.team_abbrev is not null
group by s.year, t.team_abbrev
having c > 1
order by s.year, cast(t.team_abbrev as int);
```

Findings:

- Duplicate NFL.com team ids exist in 2010-2024.
- Some duplicates are empty stale rows and can likely be deleted.
- Some duplicates both have matchup/roster/transaction references and require
  deliberate repointing, not blind deletion.

## Stepwise Repair Plan

### Step 1: Preserve the prevention fix

Keep the `_upsert_teams` rename fix and its regression test. Before merging,
rerun:

```bash
uv run pytest
uv run ruff check src tests scripts/merge_owner_identities.py
uv run ruff format --check src tests scripts/merge_owner_identities.py
uv run mypy src
```

Acceptance:

- Full gate stays green.
- A current-season NFL.com sync no longer creates a duplicate row when a team
  name changes but the NFL.com team id stays the same.

### Step 2: Build a duplicate-team audit command or script

Add a non-mutating script first, for example:

```bash
uv run python scripts/audit_duplicate_teams.py
```

The report should group by `(season_id, team_abbrev)` and show:

- candidate canonical team id
- duplicate team ids
- matchup counts
- roster counts
- transaction counts
- owner id / owner display name
- whether each duplicate is empty, single-week-only, or fully referenced

Acceptance:

- The script reproduces the live duplicate list.
- Every duplicate group is classified before any repair runs.

### Step 3: Add a dry-run duplicate-team repair script

Add a conservative repair script with `--apply` required for mutation:

```bash
uv run python scripts/repair_duplicate_teams.py --dry-run
uv run python scripts/repair_duplicate_teams.py --apply
```

Repair policy:

- Prefer the team row with the most matchup + roster references as canonical.
- Repoint `matchups.team_id`, `matchups.opponent_team_id`,
  `team_rosters.team_id`, `transactions.team_id`, and
  `transactions.counterpart_team_id`.
- Preserve useful display names as aliases only if a future schema supports team
  aliases; otherwise keep the canonical row's season-correct name from standings.
- Delete duplicate rows only after all foreign keys are repointed.
- Always create a SQLite backup before `--apply`.

Acceptance:

- No `(season_id, team_abbrev)` duplicate groups remain.
- Each completed historical season has the expected 12 teams.
- No orphaned `matchups`, `team_rosters`, or `transactions` references exist.

### Step 4: Re-run reconstruction and reconciliation

After repair:

```bash
uv run ff-pipeline reconstruct --start 2010 --end 2025 --force
uv run ff-pipeline rescore
uv run ff-pipeline verify --season 2024 --week 1 --reconcile
uv run ff-pipeline verify --season 2010 --reconcile
```

Acceptance:

- The 2024 week-1 `no_starters_recorded` row disappears.
- Remaining deltas are classified as known scoring-source gaps, especially
  long-TD-length bonuses, DST source gaps, or documented identity gaps.
- `docs/10_OPEN_QUESTIONS.md` is updated with the new trust-check result.

### Step 5: Hand off stable inputs to `dz-dashboard`

Only after Step 4 should the dashboard treat historical totals as final.

Dashboard integration inputs already confirmed:

- Use `docs/06_API_CONTRACT.md` as the only backend contract.
- Consume the FastAPI OpenAPI schema from `/openapi.json`.
- Set season-schedule switch years from
  `docs/PRE2016_STRUCTURAL_REFERENCE.md`:
  - 2011: 14 regular weeks to 13 regular weeks
  - 2021: 13 regular weeks to 14 regular weeks
- No divisions/conferences exist in any era.
- Owner views must support active and inactive managers separately.

Dashboard acceptance:

- Owner aggregate pages handle historical/inactive managers.
- Rivalry grids can render active-only and all-time views distinctly.
- Season schedule logic uses the confirmed 2011 and 2021 switch years.
- Dashboard does not depend on private SQLite internals; it uses the HTTP API.

## Recommended Next Commit Scope

Keep the next commit scoped to:

- owner identity override table and migrations
- one-owner-multiple-teams schema change
- playoff bracket consolation classification
- NFL.com team-id upsert prevention fix
- tests and docs above

Do not include a live DB repair in the same commit. The repair should be its own
audited operation with a backup and a before/after reconciliation report.

## Owner-identity & Phantom-team Repair (executed 2026-06-09)

Run via `scripts/repair_owner_identity_and_phantom_teams.py --apply` (dry-run by
default; backs up the SQLite file before committing). Addresses the dashboard
handoff `ff-pipeline-owner-identity.md`. **Per-season NFL.com
`/history/{year}/owners` was used as source of truth and corrected three of the
handoff's premises:**

- **mike (user 167650) did not play 2016-2017.** He held franchise 3 in
  2010-2015, sat out 2016 (Adam) and 2017 (ill), and returned as franchise 12 in
  2018+. His empty 2016/2017 rows were back-projection phantoms and were removed,
  not reconstructed.
- **The two "Dan"s have distinct logins**, not one shared `179898`: DJ
  (`179898`, franchise 5) and Cheese (`7655244`, franchise 7). Owner 17 was
  mis-stamped with DJ's id, which is why the 2025 re-point misassigned DJ's team.
- **DJ played 2025** (franchise 5, "The Princess McBride"); the mis-ownership was
  a symptom of the shared-id bug, not DJ's absence.

What the repair did:

- Renamed owner 5 -> `DJ`, owner 17 -> `Cheese`; corrected owner 17's
  `nfl_user_id` to `7655244`; seeded durable `owner_identity_overrides`
  (`179898`->DJ, `7655244`->Cheese) so the names survive `reconstruct-owners`.
- Re-owned 2015 "Batesohardithurts" (owner 18 -> 3) and 2025 "The Princess
  McBride" (owner 17 -> 5).
- Merged 26 `final_rank IS NULL` franchise-duplicate phantom rows (`team_id`
  193-222). 14 carried the franchise's real week-1 roster snapshot, which was
  moved onto the ranked survivor (opponent/transaction refs re-pointed, the
  duplicate week-1 matchup dropped); the 12 childless orphans were deleted.

Durable code fix: `_team_id_lookup` now prefers the ref-rich row when an abbrev
is duplicated, so a re-run resolves to the real franchise row instead of a
phantom (covered by `tests/unit/test_team_id_lookup.py`).

Before / after (`data/fantasy.db`), all handoff acceptance checks pass:

| Check | Before | After |
| --- | --- | --- |
| Played seasons not having exactly 12 ranked teams | 15 | 0 |
| `final_rank IS NULL` rows in played seasons | 26 | 0 |
| Distinct managed (owner, season) pairs | 194 | 192 |
| Owners named `Dan` | 2 | 0 (DJ + Cheese, distinct) |
| 2025 "The Princess McBride" owner | 17 (Cheese) | 5 (DJ) |

Backups: `data/backups/fantasy.db.pre-phantom-owner-repair-*.bak` and
`data/backups/fantasy-pre-identity-phantom-repair-*.db`. Foreign-key integrity
(`PRAGMA foreign_key_check`) is clean and the repair script is idempotent.

Once `dz-dashboard` confirms, its temporary presentation overrides
(`analytics/owner_identity.py`, the phantom filters in `analytics/standings.py`,
`tests/test_owner_identity.py`) become no-ops and can be removed.

## Mike Slot (team_abbrev) Back-projection Repair (executed 2026-06-10)

Run via `scripts/archive/repair_mike_slot_backprojection.py --apply` (dry-run by
default; backs up the SQLite file before committing). Addresses the dashboard
handoff `ff-pipeline-team-slot-backprojection.md` — the residual the owner-identity
repair above re-owned but did not fully relabel.

The lost-team reconstruction restored mike's (owner 3) 2010-2015 team rows but
stamped them with his *modern* franchise identity: `team_abbrev = "12"` (his
current slot) and `team_name = "Batesohardithurts"` (his 2025 name), when his real
slot those years was **3**. That produced, in each of 2010-2015, a duplicate
`team_abbrev = "12"` (mike + the real slot-12 owner) and a missing slot 3 — which
mislabeled mike's team on every dashboard surface (e.g. `/standings/2015` rendered
both mike and Kevin as "Bruce Jenner DJ's").

What the repair did — for mike's six rows (`team_id` 23, 35, 47, 59, 71, 83) set
`team_abbrev` `"12"` -> `"3"` and `team_name` to the period-correct NFL.com slot-3
name (cross-checked against the dashboard's `(year, slot) -> name` table, whose
surviving slot-12 name already matched the non-mike DB row each year):

| year | team_id | slot-3 name |
| --- | --- | --- |
| 2010 | 23 | ThisTeamMakesSullyNervous |
| 2011 | 35 | IAMTHESACKO |
| 2012 | 47 | Sulladismichaelbushleague |
| 2013 | 59 | Salty Caramel Sullad |
| 2014 | 71 | IStoleSulladsPick |
| 2015 | 83 | Snow and Mirrors |

Only `teams.team_abbrev` and `teams.team_name` changed; matchups, rosters,
transactions, draft position, and `final_rank` key on `team_id` and stayed
attached. mike's 2018-2025 rows were left alone (slot 12 is correct there).

Before / after (`data/fantasy.db`), all handoff acceptance checks pass:

| Check | Before | After |
| --- | --- | --- |
| Duplicate `team_abbrev` in a played season | 6 (2010-2015) | 0 |
| Played seasons 2010-2015 holding slot 3 | 0 | 6 |
| mike's 2015 team | `12` / "Batesohardithurts" | `3` / "Snow and Mirrors" |

Backup: `data/backups/fantasy-pre-mike-slot-repair-*.db`. `PRAGMA
foreign_key_check` is clean and the repair script is idempotent (re-runs are
no-ops). No dashboard change is required — its `(year, team_abbrev)` name overlay
now resolves mike to the period-correct slot-3 name automatically.
