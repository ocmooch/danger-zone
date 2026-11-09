# S3 — Platform-era schema (plan)

Build contract for `02_PIPELINE_BUILD_PLAN.md` §S3. Design source: `01_PIPELINE_ARCHITECTURE.md`
§5 (schema additions), §7 (identity), §8 (lifecycle); migration policy `03` §9; decisions D-2, D-4
(`00`).

**Status:** PLAN approved 2026-11-09 (all decisions S3-D1..D7 accepted as recommended). BUILD in progress
on `feature/s3-platform-era-schema`.

## Goal

Add the schema that Sleeper seasons need, as **one additive migration**, without changing a single
legacy value. This milestone only adds schema: it loads no Sleeper data (S5), maps no owners (S4)
and moves no stub rows (S4, via `legacy amend`, per S1 decision 4).

## Starting state (verified 2026-11-09, live DB)

- Alembic head `a7c9e1b3d5f2` (S2 `source_payloads`). SQLite 3.45.1 (native `DROP COLUMN`).
- `seasons` already has `is_sealed / sealed_at / seal_id` (S1), so those are **not** re-added.
- `transactions.extra_data` already exists. `teams`, `matchups` and `players` have no `extra_data`.
  `teams.division` does not exist.
- Guard triggers (S1) sit on `teams`, `matchups`, `transactions`, `players`, `seasons`,
  `team_rosters` and others (41 total). Fingerprints hash an **explicit** legacy column list
  (`legacy/spec.py`), so new columns cannot change them.
- `alembic/env.py` uses `render_as_batch=True`. A batch operation **rebuilds the table**, which would
  silently drop that table's guard triggers. S1 added columns with plain `op.add_column`.
- Season 2026 (`season_id` 17) is the NFL.com placeholder: 12 teams + 193 week-1 roster rows, no
  matchups or transactions, unsealed (D-2). S3 only creates the empty quarantine table; S4 moves
  the rows.

## Design

### 1. Columns on existing tables (all nullable or defaulted, via `op.add_column`)

| Table | Column | Type | Legacy rows get |
|---|---|---|---|
| `seasons` | `platform` | TEXT NOT NULL DEFAULT `'nfl_com'` | `nfl_com` |
| | `platform_league_id` | TEXT | NULL (D-4: `leagues.league_id` stays the franchise key) |
| | `platform_draft_id` | TEXT | NULL |
| | `settings_snapshot` | JSON | NULL |
| `teams` | `platform_team_key` | TEXT | NULL (`sleeper:roster:<roster_id>` later) |
| | `division` | TEXT | NULL (the BFF keeps deriving legacy divisions) |
| | `extra_data` | JSON | NULL |
| `matchups` | `platform_matchup_key` | TEXT | NULL (`nfl_com_game_id` stays) |
| | `extra_data` | JSON | NULL |
| `transactions` | `platform_transaction_id` | TEXT | NULL |
| | `status` | TEXT | NULL (legacy rows were all executed) |
| `players` | `extra_data` | JSON | NULL |

`team_rosters` gets no columns (Sleeper uses `extra_data` keys only, `01` §5.1).

### 2. New tables

| Table | Columns (beyond `created_at`/`updated_at`) | Keys |
|---|---|---|
| `owner_platform_identities` | `identity_id` PK, `owner_id` FK owners, `platform`, `external_user_id`, `external_display_name`, `first_season`, `last_season` (int years), `notes` | UNIQUE(`platform`, `external_user_id`) |
| `playoff_brackets` | `bracket_id` PK, `season_id` FK, `bracket` (`winners`/`losers`), `round`, `match_no`, `team1_id`/`team2_id`/`winner_team_id`/`loser_team_id` FK teams, `placement`, `team1_from`/`team2_from` JSON, `source` | UNIQUE(`season_id`, `bracket`, `round`, `match_no`) |
| `traded_picks` | `traded_pick_id` PK, `league_id` FK, `pick_season`, `round`, `original_owner_id`/`current_owner_id`/`previous_owner_id` FK owners, `observed_at`, `source_transaction_id`, `extra_data` JSON (raw roster ids) | UNIQUE(`league_id`, `pick_season`, `round`, `original_owner_id`) (decision S3-D6) |
| `player_status_snapshots` | `snapshot_id` PK, `player_id` FK, `as_of_date`, `source`, `injury_status`, `injury_body_part`, `injury_start_date`, `practice_participation`, `depth_chart_position`, `depth_chart_order`, `nfl_team`, `status`, `extra_data` JSON | UNIQUE(`player_id`, `as_of_date`, `source`) |
| `legacy_nfl_com_2026_preseason` | `quarantine_id` PK, `source_table`, `original_pk`, `row_data` JSON (verbatim row), `quarantined_at`, `amendment_id` | UNIQUE(`source_table`, `original_pk`) (decision S3-D3) |

None of the new tables is legacy-guarded: they hold no sealed-season rows. The quarantine table is
written once by S4's audited amendment.

### 3. Indexes

- `uq_teams_season_platform_key`: UNIQUE(`season_id`, `platform_team_key`) **WHERE
  `platform_team_key` IS NOT NULL** (partial; `sqlite_where` + `postgresql_where`).
- `uq_transactions_platform_leg`: UNIQUE(`season_id`, `platform_transaction_id`,
  `COALESCE(player_id, -1)`, `COALESCE(team_id, -1)`, `COALESCE(direction, '')`) WHERE
  `platform_transaction_id IS NOT NULL` (an expression index, decision S3-D2). FAAB-only and
  pick-only trade legs have a NULL `player_id`, and plain NULLs never collide in a UNIQUE index.
  That would let a re-load duplicate those legs.
- `ix_matchups_platform_key` on `platform_matchup_key` (lookup; uniqueness already comes from
  (`season_id`, `week`, `team_id`)).
- `ix_seasons_platform` on (`platform`, `year`).

### 4. Migration mechanics (`s3_platform_era_schema`)

- `upgrade()`: `op.add_column` per column (native `ALTER TABLE ADD COLUMN`, no rebuild), then
  `op.create_table` ×5, then the indexes. **No `batch_alter_table` anywhere.**
- `downgrade()`: drop the indexes, then the new tables, then each added column with native
  `ALTER TABLE … DROP COLUMN` (SQLite ≥ 3.35). This deliberately avoids a batch rebuild, so S1
  triggers survive a downgrade. Downgrade asserts the SQLite version and fails loudly if it is
  older.
- `platform` has no CHECK constraint (S3-D5). Allowed values live in code
  (`Literal["nfl_com", "sleeper"]` on the ORM), so a future platform never needs a rebuild of a
  guarded table.

### 5. ORM (`repository/models.py`)

New mapped columns on `Season`, `Team`, `Matchup`, `Transaction` and `Player`. New classes
`OwnerPlatformIdentity`, `PlayoffBracket`, `TradedPick`, `PlayerStatusSnapshot` and
`LegacyNflCom2026Preseason`, all strictly typed. `test_migration_creates_every_modeled_table`
keeps the models and the migration in lockstep.

### 6. Docs

`docs/04_DATA_MODEL.md`: the new columns, the 5 new tables, and `source_payloads` (missed in S2).
`02` §S3 marked done at the end.

### 7. Consumers (dz-dashboard)

The BFF imports `ff_pipeline.repository` models. Once `dev` carries the new ORM columns, a
dashboard pointed at an **un-migrated** DB fails with "no such column". So the live DB is migrated
**before** the PR merges (as S1 did), and the dashboard gate runs against the migrated DB.

## Tests

- `tests/integration/test_migration_s3.py` on the S1 sealed fixture DB (`_build`):
  - upgrade: fingerprints identical, `legacy verify --full` ok, guard trigger count unchanged,
    legacy seasons read `platform = 'nfl_com'`, every new column NULL on legacy rows;
  - downgrade to `a7c9e1b3d5f2`: new columns and tables gone, **triggers still present** (count
    and names), fingerprints identical, verify ok; then upgrade again;
  - after the round trip, a forced `UPDATE matchups SET team_score = 0` on the sealed season is
    still refused.
- Index behavior: two Sleeper teams with the same `platform_team_key` in one season are refused,
  while NULL keys (legacy) don't collide; a duplicate Sleeper transaction leg is refused, including
  a NULL-`player_id` FAAB leg; NFL.com rows (NULL `platform_transaction_id`) are unaffected.
- ORM round trip: insert and read one row in each new table (FKs, JSON columns, unique keys).
- Existing `test_alembic_migrations.py` (every modeled table, idempotent upgrade, round trip) and
  `test_legacy_guard.py` (pinned S1 round trip) stay green.

## Rollout (BUILD session, with the user)

1. `sqlite3 .backup` of the live DB to scratch; `legacy verify`; `migrate up`; `legacy verify`;
   record the fingerprints-unchanged line for the PR body.
2. dz-dashboard gate against the migrated copy (backend pytest + the live-GET smoke used in S1).
3. **Ask**, then migrate the live DB, `legacy verify`, check that the `dz-sleeper-capture` wrapper
   still runs, and only then merge the PR.

## Done when

- [x] Live DB at the new head (`b8d0f2a4c6e3`, 2026-11-09); migration + ORM merged to `dev` (PR #71).
- [x] `legacy verify` identical before/after on the seal-v1 copy and on the live DB; "fingerprints
      unchanged" recorded in the PR body.
- [x] Round-trip test proves the triggers survive the downgrade.
- [x] Full danger-zone gate green; dz-dashboard backend suite + live-GET smoke green against the
      migrated DB.
- [x] `04_DATA_MODEL.md` updated (incl. `source_payloads`); `02` §S3 marked done.

## As built (2026-11-09)

Code: `repository/models.py` (new columns on `Season`/`Team`/`Matchup`/`Transaction`/`Player`;
`OwnerPlatformIdentity`, `PlayoffBracket`, `TradedPick`, `PlayerStatusSnapshot`,
`LegacyNflCom2026Preseason`; `Platform` literal), migration `b8d0f2a4c6e3_s3_platform_era_schema`,
`tests/integration/test_migration_s3.py`, `docs/04_DATA_MODEL.md`. Built as designed; no
deviations.

### Rollout

- **Copy** (`sqlite3 .backup` of live): verify PASSED → `migrate up` → PASSED; 41 → 41 triggers;
  all 17 seasons `platform = 'nfl_com'`.
- **Consumers:** danger-zone gate 740 passed. dz-dashboard backend suite 494 passed (it imports the
  S3 ORM through its editable `../danger-zone` install). A second BFF on :8801 over the migrated
  copy vs the live BFF on :8800: all 53 GET routes gave the same status (48×200, 5×400 param
  validation) and **byte-identical bodies**.
- **Live** (approved): verify PASSED → migrate → PASSED; 41/41 triggers; sealed 2010–2025;
  running BFF still 200; `sleeper-league capture --dry-run` OK.
- **Lesson:** because dz-dashboard installs `../danger-zone` editable, the dashboard imports whatever
  branch is **checked out**, not just `dev`. Any schema branch therefore needs the live migration
  before the BFF next restarts, not only before merge. Here the BFF had loaded the old models at
  startup (no `--reload`), so nothing broke.

## Known issues (out of scope)

- `prune-players` FOREIGN KEY failure on 5 players; `ruff format` debt in
  `crawlers/adp/sleeper.py` + 3 scripts/alembic files; vault-staleness warning (S9).
- Per-request debug lines in console logging at `LOG_LEVEL=WARNING` (noted in S2).
- dz-dashboard `CLAUDE.md` says `uv run pytest tests/dashboard`, but its tests live in `tests/`.

## Decisions (defaults approved by the user 2026-11-09)

| # | Decision | Recommended default | Alternative |
|---|---|---|---|
| S3-D1 | How columns are added and removed on guarded tables | Native `ADD COLUMN` / `DROP COLUMN` only, never batch, plus a test that the triggers survive. | Batch + re-create triggers in the migration (more code; one forgotten trigger = silent loss of protection). |
| S3-D2 | Idempotency key for Sleeper transaction legs | Partial **expression** unique index with `COALESCE` on `player_id` / `team_id` / `direction`, so FAAB/pick legs (NULL player) dedupe too. | The plain column index from `01` §5.1 (NULL legs could duplicate on re-load). |
| S3-D3 | Shape of `legacy_nfl_com_2026_preseason` | One generic table: `source_table`, `original_pk`, verbatim `row_data` JSON, `amendment_id`. It keeps every column even if `teams`/`team_rosters` grow later. | Two typed mirror tables (`…_teams`, `…_rosters`) that must track schema changes. |
| S3-D4 | Should new columns on **sealed** rows be guarded? | No. They hold no legacy data and are outside the seal manifest; S5's loader refuses sealed seasons, and the default `platform = 'nfl_com'` is set by the migration itself. | Extend the guard spec, which needs a seal-v2 manifest and a re-adoption: heavy, no benefit now. |
| S3-D5 | `seasons.platform` validation | Code-level `Literal` only, no CHECK constraint (a CHECK can't be changed later without rebuilding a guarded table). | SQL CHECK (`'nfl_com','sleeper'`). |
| S3-D6 | `traded_picks` keys | Owners (FK `owners`, stable across seasons) rather than teams (future seasons have no team rows yet); raw Sleeper roster ids kept in `extra_data`; UNIQUE(`league_id`, `pick_season`, `round`, `original_owner_id`). | Roster-id columns (meaningless across renewals until roster_id stability is verified in 2027). |
| S3-D7 | Live rollout order | Copy → dashboard check → ask → live migration **before** merging (so dz-dashboard on `dev` never meets an un-migrated DB). | Merge first, migrate after. |
