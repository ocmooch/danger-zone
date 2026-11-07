# S1 — Legacy guard (plan)

Status: **BUILT 2026-11-07** (rehearsed on a copy of the live DB; live rollout pending user approval) · branch `feature/s1-legacy-guard` · written 2026-11-07
Source docs: `docs/sleeper/02_PIPELINE_BUILD_PLAN.md` §S1, `docs/sleeper/03_LEGACY_PRESERVATION.md`
§3, §5–§9. This plan is the build contract; where it deviates from those docs it says so.

## Goal

The 2010–2025 seasons in the live DB cannot change, by any writer (ORM, raw SQL, the `sqlite3`
shell), except through one audited path (`legacy amend`). Re-derivable data (stats, scoring,
projections, nflverse) keeps refreshing on those years.

## Starting state (verified 2026-11-07)

- Alembic head `d4e6f8a0b2c4`; no triggers in the live DB; no preflight hook exists yet.
- Live DB fingerprints == `docs/sleeper/seals/seal-v1.manifest.json` for all 14 tables (0 changed).
- Fingerprint code lives only in `scripts/vault/verify.py` (stdlib, ships inside the vault).
- Seasons: 2010–2025 `completed`; 2026 = NFL.com placeholder, `in_progress` (D-2).

## Design

### 1. What is guarded (from the seal-v1 manifest, unchanged)

Guards protect **only the legacy columns** listed in the seal manifest — the same set the
fingerprint covers. Columns added by later additive migrations stay writable on legacy rows, so
S3 backfills (e.g. `seasons.platform = 'nfl_com'`) work without an amendment.

| Kind | Tables | INSERT | UPDATE OF legacy cols | DELETE |
|------|--------|--------|-----------------------|--------|
| Season-scoped | `teams`, `matchups`, `transactions`, `scoring_rules` (via `season_id`); `team_rosters` (via `team_id → teams.season_id`); `player_availability` (via `season_year`: blocked if any sealed season has that year) | blocked into sealed season | blocked | blocked |
| The seal row itself | `seasons` | — | blocked when `OLD.is_sealed = 1` (also covers `is_sealed`, `sealed_at`, `seal_id`, so un-sealing needs an unlock) | blocked when sealed |
| Global identity | `leagues`, `owners`, `commissioners`, `owner_identity_overrides`, `player_id_overrides`, `player_identity_links` | **allowed** (new owners/overrides in the Sleeper era) | blocked for rows that exist in the seal | blocked |
| Column-scoped | `players.nfl_com_player_id` | allowed | blocked when `OLD.nfl_com_player_id IS NOT NULL` and value changes | blocked when `OLD.nfl_com_player_id IS NOT NULL` |

"Rows that exist in the seal" for global tables = a `legacy_sealed_rows(table_name, pk)` registry
filled at seal time (small: ~130 rows). Triggers check membership; new rows are never in it.

Every block is bypassed only while `legacy_guard.unlocked_until` (UTC ISO text) is in the future.

### 2. Schema (one additive alembic migration, `s1_legacy_guard`)

- `seasons`: `is_sealed BOOLEAN NOT NULL DEFAULT 0`, `sealed_at TEXT NULL`, `seal_id TEXT NULL`.
- `legacy_guard(id=1 CHECK, unlocked_until TEXT, unlocked_reason TEXT)` — seeded with one locked row.
- `legacy_seals(seal_id PK, created_at, kind, git_commit, alembic_revision, manifest_sha256,
  manifest JSON)` — the fingerprint baseline `legacy verify` compares against.
- `legacy_sealed_rows(table_name, pk TEXT, PRIMARY KEY(table_name, pk))`.
- `legacy_amendments(amendment_id PK, created_at, reason, command, git_commit, before_seal,
  after_seal, tables_changed JSON, rows_changed JSON)` — append-only (its own UPDATE/DELETE
  triggers always abort).
- All guard triggers, as **literal SQL in the migration** (migrations stay frozen). A unit test
  asserts the migration's DDL equals what `legacy/triggers.py` generates, so the two can't drift.
- `downgrade()` drops triggers + new tables + columns (batch mode); upgrade→downgrade→upgrade
  leaves fingerprints identical (tested).

**Deviation from 02 §S1:** the migration does **not** flip `is_sealed`. Sealing is a separate,
explicit step: `ff-pipeline legacy seal --adopt docs/sleeper/seals/seal-v1.manifest.json`
verifies the live DB against the committed manifest, then writes `legacy_seals`, fills
`legacy_sealed_rows`, and sets `is_sealed = 1` for 2010–2025. Keeps migrations cheap on test DBs
and makes the "fingerprints matched" check visible.

### 3. Code layout

```
src/ff_pipeline/legacy/
  fingerprint.py   canonical serializer + fingerprint_table (same algorithm as vault verify.py)
  spec.py          guarded tables, linkage, legacy columns (loaded from the seal manifest)
  triggers.py      DDL generator for the guard triggers + list of expected trigger names
  verify.py        quick / full verification against legacy_seals; locatable diffs
  amend.py         unlock window, pre-amend backup, run command, diff, audit row, new seal
  cli.py           `legacy` Typer sub-app: adopt/seal, verify, status, amend
src/ff_pipeline/repository/legacy_guard.py   SealedSeasonError, assert_writable(session, season_id)
src/ff_pipeline/repository/models.py         Season columns; LegacyGuard/LegacySeal/LegacyAmendment/LegacySealedRow
src/ff_pipeline/cli.py                       register sub-app; preflight in run/backfill/reconstruct/rescore/migrate up
```

`scripts/vault/verify.py` stays self-contained (it must run inside a vault with no package
installed). A parity unit test runs both implementations on the same fixture DB and asserts
identical output.

### 4. CLI

| Command | Does |
|---------|------|
| `legacy seal --adopt <manifest>` | One-time: verify live == manifest, store baseline, flag 2010–2025 sealed |
| `legacy verify [--quick]` | Quick: trigger presence + row counts + per-table hashes. Full: + per-season hashes + `integrity_check`. Prints table, season, first differing PKs. Never repairs. Exit 1 on mismatch |
| `legacy status` | Seal id/date, sealed years, lock state, last amendment |
| `legacy amend --reason "…" -- <cmd…>` | Clean verify → named backup `fantasy-pre-amend-<slug>-<ts>.db` (outside pruning) → unlock 10 min → run cmd → relock (always, in `finally`) → fingerprint diff → `legacy_amendments` row → new seal baseline `seal-vN+1` in `legacy_seals` |

Preflight: `run`, `backfill`, `reconstruct`, `rescore`, `migrate up` call `verify --quick` first and
abort on mismatch. If no seal has been adopted yet (fresh/test DB) the preflight is a no-op.

**Deferred (explicitly):**
- Producing and uploading the **vault archive** for seal-vN+1 after an amendment → S9 (rolling
  seal). S1's `amend` records the new baseline in the DB and prints the follow-up command
  (`scripts/vault/build_vault_dir.py` + `vault_sync.sh`); no amendment is expected before S4.
- `legacy restore-drill` → S9. It needs a private key, so it stays user-run;
  `scripts/vault/restore_check.sh` already covers it.

### 5. Existing commands that write guarded data

From a code sweep, these write guarded tables: the NFL.com crawlers (`backfill` NFL.com path,
`history.py`, `league.py`), `reconstruct`, `reconstruct-owners`, `scoring load`, `avatars`
(teams avatar columns), `prune-players`, `overrides/hamlin_2022_wk17.py`. On sealed years they
**should** now be refused (they'd be amendments). The player resolver only fills NULL IDs and never
overwrites/deletes, so Sleeper/nflverse resolution is compatible.

Build task: run every CLI command that touches the DB against a **guarded copy** of the live DB,
and record each as *keeps working* / *refused on sealed years (intended)* / *broken → fix*.
Must keep working: `run --source nflverse`, `rescore --season 2015`, `backfill-projections`,
`injury-reports`, `team-defense`, `status`, `verify`, `backup`, `export`, `serve`.

## Tests

Unit (`tests/unit/`):
- `test_legacy_fingerprint.py` — parity with `scripts/vault/verify.py`; determinism (moved from
  `test_vault_fingerprint.py` where it overlaps).
- `test_legacy_triggers.py` — generated DDL == migration DDL; trigger-name list complete.
- `test_legacy_guard.py` — `assert_writable` raises `SealedSeasonError` naming season + amend command.

Integration (`tests/integration/test_legacy_guard.py`, tiny sealed fixture DB):
- UPDATE / DELETE / INSERT on a sealed season raise, for each season-scoped table; same on an
  unsealed season succeed.
- Global tables: INSERT of a new owner/override succeeds; UPDATE/DELETE of a sealed row raises.
- `players`: changing a set `nfl_com_player_id` raises; filling NULL and editing other columns succeed.
- Updating a non-legacy column on a sealed row succeeds.
- Un-sealing (`is_sealed = 0`) raises without an unlock.
- Unlock window: writes succeed inside it, fail after it expires.
- `legacy_amendments` is append-only.
- `amend` writes audit row + new baseline, and always relocks even when the command fails.
- `verify --quick` detects a dropped trigger; full verify locates a changed row (table/season/PK).
- Migration upgrade → downgrade → upgrade keeps fingerprints identical.
- `rescore` on a sealed season succeeds.

## Rollout on the live DB (BUILD session, with the user)

1. `ff-pipeline backup` (named milestone `pre-s1-legacy-guard`).
2. On a **copy**: `migrate up` → `legacy seal --adopt …` → `legacy verify` → the command sweep (§5)
   → the deliberate `UPDATE matchups … season 2015` is refused.
3. User approves → same on the live DB.
4. dz-dashboard: run its gate against the guarded live DB (new columns/tables are additive; the
   dashboard opens read-only).

## Done when

- [ ] Live DB has the guard; `legacy verify` (full) passes against seal-v1.
- [ ] `ff-pipeline run --source nflverse` and `rescore --season 2015` still succeed.
- [ ] `UPDATE matchups SET team_score = 0 WHERE season_id = <2015>` in the `sqlite3` shell is refused.
- [x] Command sweep table (§5) recorded in this file; nothing in "must keep working" is broken.
- [x] Unit + integration tests above pass (636 total, Python 3.12 locally). [ ] CI green (Python 3.14 — close every sqlite connection) — on the PR.
- [x] RUNBOOK "Legacy vault" section gains `legacy verify/status/amend`.
- [ ] `SETUP_PROGRESS`-style note not needed; `02` §S1 marked done; PR → `dev`.

## As built (2026-11-07)

Code: `src/ff_pipeline/legacy/` (`spec`, `fingerprint`, `triggers`, `verify`, `seal`, `amend`,
`cli`), `repository/legacy_guard.py`, migration `e2a4c6e8f0b1_s1_legacy_guard` (41 triggers).

### Deviations from the design above

1. **Verify compares a sealed-scope baseline, not the whole-table manifest.** The committed
   seal-v1 manifest hashes every row (incl. 2026 rows and all 4,286 players), but new owners,
   new players and 2026 writes are allowed — so the live tables legitimately drift from it.
   `legacy seal --adopt` checks the live DB against the committed manifest **exactly**
   (whole tables), then stores a baseline over the sealed scope only: rows of sealed seasons,
   plus registered global rows. It also asserts the sealed seasons' per-season hashes equal the
   committed manifest's (same rows, same algorithm). `legacy verify` compares against that.
2. **`legacy_sealed_rows` holds every sealed row (112,279), with a 16-hex row hash**
   (`row_hash` column; WITHOUT ROWID). The design had ~130 global rows only. Without per-row
   data, verify cannot "print the first differing PKs"; the hashes make that exact
   (changed / missing / added). Table and season hashes stay full sha256.
3. **`players` guard uses the registry** (the 1,875 players with an `nfl_com_player_id` at seal
   time) instead of `OLD.nfl_com_player_id IS NOT NULL`. Same rows on the live DB, but inert on
   a fresh/test DB with no seal.
4. **UPDATE guards fire only when a guarded value actually changes** (`NEW.c IS NOT OLD.c`),
   rather than on `UPDATE OF <cols>`. Idempotent re-writes of identical values (upsert re-runs)
   pass; any real change is refused.
5. **`team_rosters` checks both links** (`team_id → teams.season_id` *or* `season_year`), so a
   row can't slip into a sealed year through either column.
6. **Also guarded:** `legacy_seals` is append-only like `legacy_amendments`;
   `legacy_sealed_rows` changes only inside an unlock.
7. **Hamlin 2022 override** no-ops when 2022 is sealed (its result is already in the seal), so
   `rescore`/`run` never try to rewrite sealed rows. Unit test added.
8. Sealing/re-seal logic lives in `legacy/seal.py` (not listed in §3). Full verify does not hash
   assets (03 §7 mentions it; the vault's `verify.py` covers asset bytes).
9. The amend backup goes to `data/backups/amendments/` — pruning only scans the top level of
   `data/backups/`, so it is never pruned.

### Command sweep (§5) — on a guarded copy of the live DB, 2026-11-07

Copy: `sqlite3 .backup` of `data/fantasy.db` → `migrate up` → `legacy seal --adopt` (PASSED,
112,279 rows) → full verify 2.3 s, quick 1.6 s (the preflight cost).
`sqlite3` shell: `UPDATE matchups SET team_score = 0 WHERE season_id = <2015>` →
`legacy_guard: matchups row belongs to a sealed season; use ff-pipeline legacy amend`.
Un-sealing 2015 and deleting a sealed owner were refused the same way.

| Command | Result |
|---------|--------|
| `run --source nflverse` | **keeps working** (players +176 ~3,732, stats +3,007) |
| `rescore --season 2015`; `rescore` (all) | **keeps working** (Hamlin override skips sealed 2022) |
| `backfill-projections --season 2015` | **keeps working** |
| `injury-reports --season 2015` | **keeps working** |
| `team-defense --season 2015` | **keeps working** |
| `status`, `backup`, `serve` | **keep working** (`/health`, `/seasons/{id}`, standings, teams → 200) |
| `verify --season 2015 --reconcile` | **unchanged** — 178 pass / 6 fail, exit 1, identical on the unguarded DB (pre-existing drift) |
| `export` | unchanged stub (exit 64) |
| `scoring load` into a sealed year | **refused (intended)** — `legacy_guard: scoring_rules row belongs to a sealed season` |
| `prune-players` | **pre-existing failure, not S1** — `FOREIGN KEY constraint failed` on the same 5 players with the old code on an unguarded copy; `--dry-run` works |
| `reconstruct`, `reconstruct-owners`, `draft`, `avatars`, `run/backfill --source nfl_com` | **refused on sealed years (intended)**; not run live (NFL.com cookie + network). The NFL.com league upsert also writes the sealed `leagues` row, so a default `run` (which still includes `nfl_com`) would be refused if NFL.com reports a new season — retire `nfl_com` from the default `run` in the Sleeper work. |

After the sweep, full `legacy verify` still PASSED against seal-v1.

## Decisions (defaults approved by the user 2026-11-07)

1. Global identity tables: allow INSERT of new rows, block changes to sealed rows — **default yes**.
2. Sealing via an explicit `legacy seal --adopt` step instead of inside the migration — **default yes**.
3. Vault upload of seal-vN+1 and `restore-drill` deferred to S9 — **default yes**.
4. 2026 NFL.com placeholder stays **unsealed** in S1; S4 still moves it via `legacy amend` so the
   move is audited (D-2).
