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
