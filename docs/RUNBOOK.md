# Runbook

Operational playbook for the day-2 life of the Phase 1 pipeline. Read this when something has gone wrong (or might). For "how is this designed" go to [`02_ARCHITECTURE.md`](02_ARCHITECTURE.md); for the canonical exit-code table and CLI surface go to [`08_OPERATIONS.md`](08_OPERATIONS.md).

The pipeline is idempotent end-to-end. The right answer to almost every "did this run cleanly?" question is "re-run it and see."

---

## At-a-glance triage

```bash
ff-pipeline status --verbose       # last run, per-source health, recent failures
tail -n 200 data/logs/pipeline.log | jq 'select(.level != "info")'
ls -lh data/backups/ | tail -5     # most recent backup point
```

If `status` looks fine but data is wrong, jump to [Scoring or stats look wrong](#scoring-or-stats-look-wrong).
If `status` shows a failure, find the matching section below.

---

## Cookie expired

**Signal**: any command targeting NFL.com exits **77** (`EX_NOPERM`). Logs include `nfl_com.auth_failure`. Cron output shows the same exit code in `data/logs/cron.log`.

**Fix**:

1. Log into [fantasy.nfl.com](https://fantasy.nfl.com) in your browser.
2. DevTools → Application (or Storage) → Cookies → `https://fantasy.nfl.com`.
3. Copy the full Cookie header. (Easiest: Network tab → any request → "Copy as cURL" and grab the `-H 'Cookie: ...'` segment.)
4. Update `.env`:
   ```bash
   ff-pipeline cookie set                       # interactive (hidden input)
   # or, non-TTY:
   ff-pipeline cookie set --stdin < new.cookie
   ```
   The command validates the cookie against your league before persisting. A broken cookie is rejected with exit 77 and `.env` is left untouched.
5. Confirm + re-run:
   ```bash
   ff-pipeline cookie test
   ff-pipeline run --source nfl_com
   ```

**If `cookie set` exits 69** (`EX_UNAVAILABLE`): NFL.com is unreachable — network/DNS issue, not a cookie problem. Wait and retry.

---

## Mid-backfill failure

**Signal**: `ff-pipeline backfill` aborts partway through with a per-season status line in red and a "Re-run `ff-pipeline backfill` to resume." message.

**Behavior**: every season already marked `completed` in `pipeline_runs` is skipped on the next invocation. No state is rolled back; partial seasons commit row-by-row via upserts.

**Fix**:

1. Read the failed outcome — the per-source line names exactly which `(source, year)` aborted.
2. If the cause was an auth failure (exit **77**, detail contains `AuthFailureError`): refresh the cookie ([Cookie expired](#cookie-expired)), then re-run the same backfill command.
3. If the cause was a transient network failure (exit **1**, detail mentions timeout/5xx): re-run the same command. The completed years are skipped automatically.
4. To re-run a year that's already marked complete (e.g., scoring rules changed): `ff-pipeline backfill --season YYYY --force`.

**Verify**: `ff-pipeline status` shows the latest `pipeline_runs` row was `completed` and per-source health rows look sane.

---

## Reconstructing historical seasons

**When**: you need *real* per-week history for past seasons — final standings (champion / finish order), every week's matchups, and true per-week starting lineups with NFL.com player points. This is distinct from `backfill` (which fetches nflverse stats + current-state NFL.com pages); `reconstruct` reads the year-scoped `/history/{year}/...` pages that the `/owners` and `/team/{id}` pages cannot provide.

```bash
ff-pipeline reconstruct --start 2010 --end 2025
```

**Behavior**:

- Per season, in order: standings → matchups (all weeks) → per-week lineups (teamgamecenter) → matchup-derived team records. Standings sets the regular-season-week boundary that matchups uses to classify playoff weeks.
- **Resumable**: each season that finished records a `pipeline_runs(mode='reconstruct')` success row; re-running skips completed years. Use `--force` to redo one (`reconstruct --season 2018 --force`).
- A bad cookie aborts cleanly with exit **77** after committing prior seasons — refresh the cookie ([Cookie expired](#cookie-expired)) and re-run to resume.
- A full 2010–2025 run is ~2 hours at the default `NFL_COM_DELAY_SECONDS` (~2s); it is safe to background (`nohup … &`) and re-run.

**Defaults & gotchas**:

- `--end` defaults to **current year − 1**: an in-progress season has no final standings to reconstruct. Pass `--end` explicitly to dodge the off-by-one and the nflverse 404 on the unplayed current year.
- Matchup reconstruction reads the playoff bracket page to classify postseason rows as championship vs consolation. Re-run `reconstruct --force` for already-built seasons before expecting `matchups.is_consolation` to be populated in an existing DB.
- 2010–2015 now have reconstructed player scoring, but `verify --reconcile` remains the trust gate; current real-DB checks still show team-total drift that must be investigated before treating every reconstructed score as final.

**Verify**: after a run, `ff-pipeline status` shows the latest reconstruct run `success`; spot-check with the API (`/seasons`, `/matchups`, lineups) or SQL (`seasons.status='completed'`, `team_rosters` has multiple weeks per season). Then `rescore` + `verify --sweep` the scored seasons (2016–2025) since real lineups now carry `nfl_com_player_id`.

Run the maintained player-source integrity audit after any NFL.com reconstruction:

```bash
uv run python scripts/audit_source_player_identities.py --strict
```

It must report zero mismatches. For a reviewed legacy mismatch, update the
source-backed ledger and dry-run `scripts/repair_source_player_identities.py`
before applying it; the repair snapshots SQLite and is idempotent.

---

## Transactions have a blank week (`effective_week` NULL)

**Signal**: a season's `transactions` rows have `effective_week` NULL while `executed_at` is set — NFL.com left the `.transactionWeek` cell blank, so the parser stored nothing. Notably all of 2010's early-season moves: the season has full transaction data but its first *week-labeled* non-draft row is week 6, so any pre-week-6 pickup looks like a late add to week-aware consumers (e.g. the dashboard's "roster drift" flag).

**Fix**: derive the week from the date against that season's schedule.

```bash
uv run python scripts/backfill_transaction_weeks.py --dry-run   # preview counts
uv run python scripts/backfill_transaction_weeks.py             # apply
```

Idempotent — only NULL-week rows with a date are touched, and the week is the regular-season week whose games are the first on or after `executed_at` (drafts at week 0 and post-season-dated moves are left alone). Re-runnable; safe to scope with `--start/--end`.

---

## Scoring or stats look wrong

The pipeline stores **raw stats** and **scored points** separately. Diagnosing means deciding which layer is wrong.

1. **Cross-check against NFL.com**:
   ```bash
   ff-pipeline verify --player "Lamar Jackson" --season 2025 --week 5
   ```
   Output names ours vs. theirs vs. delta. The default tolerance is `SCORING_VERIFY_TOLERANCE` (0.1 points) — anything inside it passes silently.

2. **If raw stats are wrong** (deltas large + uniform across players in a week):
   - Re-fetch the source: `ff-pipeline run --source nflverse --season 2025`. The upsert overwrites stale rows; nothing else changes.
   - nflverse routinely pushes Monday-through-Wednesday stat corrections. A Wednesday-evening run usually catches them.

3. **If the scoring rules are wrong** (deltas concentrated in one stat category — e.g., all kickers off by a constant):
   - Re-load the league settings: `ff-pipeline scoring load --csv path/to/settings.csv`.
   - Dry-run a rescore: `ff-pipeline rescore --season 2025 --dry-run`. The diff list shows which rows would move.
   - Commit it: `ff-pipeline rescore --season 2025`.

4. **If the engine is wrong** (rule-by-rule deltas don't match what the engine should compute):
   - Reproduce with a unit test in `tests/unit/test_scoring_engine.py` covering the misbehaving rule.
   - Fix `src/ff_pipeline/scoring/engine.py`. Then rescore — `ff-pipeline rescore` walks every stored row.

5. **Sweep verification** across a season after any of the above:
   ```bash
   ff-pipeline verify --sweep --season 2025
   ```
   Sweeps weeks 1, 8, and 15 for every starter. Non-zero exit (`1`) = failures remain.

---

## NFL.com HTML changed (parser broke)

**Signal**: logs include `nfl_com.parse_failure` events. The affected page type is in the `parser` field; row counts in `source_health` are abnormally low.

**Fix**:

1. Enable raw-HTML capture so the failing page is on disk:
   ```bash
   echo 'SAVE_RAW_HTML=true' >> .env   # or edit in place
   ff-pipeline run --source nfl_com
   ```
   Failing responses land in `data/raw_pages/`.
2. Open the saved HTML; identify what changed in the DOM/markup.
3. Update the parser in `src/ff_pipeline/crawlers/nfl_com/parsers.py`.
4. Drop the new HTML into `tests/fixtures/nfl_com_html/` under the page-type folder.
5. Update / add a test in `tests/unit/test_nfl_com_parsers.py` (or the matching module) that pins the new fixture.
6. Run `pytest tests/unit/ -k parser` until green.
7. Re-run the pipeline; flip `SAVE_RAW_HTML=false` again to save disk.

---

## Database corruption or accidental wipe

**Signal**: `ff-pipeline init` fails with a SQLite error, or `status` shows missing tables, or you ran `rm` against the wrong file.

**Fix from backup (preferred)**:

```bash
# Identify the most recent good backup
ls -lh data/backups/
cp data/backups/fantasy-YYYY-MM-DD.db data/fantasy.db

# Catch up to current
ff-pipeline run
```

**Fix from scratch (nuclear option)**:

```bash
rm -i data/fantasy.db data/nflverse_cache/*  # confirm each
ff-pipeline init
ff-pipeline scoring load --csv path/to/settings.csv
ff-pipeline backfill --start "$LEAGUE_START_YEAR"
```

A 10-year fresh backfill takes ~45–75 minutes (NFL.com rate-limited at `NFL_COM_DELAY_SECONDS`).

**Investigate root cause**: was the SQLite file on a sync-folder (Dropbox/iCloud)? Did two `ff-pipeline run`s race? SQLite is single-writer; the schedule in `scripts/cron.example` avoids concurrent runs by design, but a manual `run` during a cron slot can collide.

---

## Bad config / `.env`

**Signal**: any command exits **4** with a "Configuration error" block listing the missing/invalid fields.

**Fix**:

1. Diff your `.env` against `.env.example`.
2. The error message names each missing/invalid key — fix them in place.
3. Most common: `NFL_COOKIE` empty (after a botched `cookie set`), `LEAGUE_START_YEAR` out of range, malformed `DATABASE_URL`.

---

## Disk filling up

**Likely culprits** (in order):

| Location | Bound | How to reclaim |
|----------|-------|----------------|
| `data/backups/` (dated dailies) | grows daily via cron | `ff-pipeline backup --keep-days 7` (rerun once with a smaller window prunes immediately) |
| `data/backups/` (`fantasy-pre-*.db` milestones) | written by repair scripts; **not** pruned by `--keep-days`, so they pile up unbounded | `ff-pipeline backup --keep-milestones 3` keeps the 3 newest and prunes the rest |
| `data/logs/` | rotated daily, 14 days kept | rotation is automatic — if it isn't, check `LOG_FILE_RETENTION_DAYS` |
| `data/raw_pages/` | unbounded when `SAVE_RAW_HTML=true` | flip the flag back to `false` and `rm -rf data/raw_pages/` |
| `data/nflverse_cache/` | grows with backfill scope | safe to delete — next run repopulates |
| `data/fantasy.db` | small, < 200 MB at 15+ seasons | not the problem — leave alone |

---

## API server returns 500s

**Triage**:

```bash
# Is the server actually running?
curl -s http://127.0.0.1:8000/health
# Tail the structured log
tail -F data/logs/pipeline.log | jq 'select(.event | startswith("api."))'
```

**Most common cause**: the database file moved or `.env` `DATABASE_URL` is stale. Restart `ff-pipeline serve` after fixing.

**Less common**: a schema migration ran without restarting the server (FastAPI holds its own engine). Restart.

**`/docs` and `/redoc`** are always live — if those load and a specific endpoint returns 500, the failure is in `repository/queries.py` for that resource. Reproduce in `tests/integration/test_api_endpoints.py` against a fresh fixture DB.

---

## "What was the state at game time?"

The `is_pre_kickoff_snapshot` / `was_locked_at_kickoff` columns are the gates. A run with no `--snapshot-kind` flag uses a UTC heuristic (in-window = `pre_kickoff`, otherwise `audit`). Force the kind explicitly when running near kickoff:

```bash
ff-pipeline run --source nfl_com --snapshot-kind pre_kickoff
ff-pipeline run --source nfl_com --snapshot-kind audit
```

Verify the snapshot landed:

```sql
SELECT week, is_pre_kickoff_snapshot, COUNT(*) AS rows
FROM player_availability
WHERE season_id = ?
GROUP BY week, is_pre_kickoff_snapshot
ORDER BY week;
```

If a `pre_kickoff` row is missing for a week the games have already played, the audit row is what you have — its `points` won't reflect the locked roster. Cron's Sunday 23:30 slot is meant to catch this; a missed slot is a permanent gap.

---

## Legacy vault

The NFL.com era (2010–2025 + the 2026 stub) is sealed as **seal-v1** (2026-11-07). Full design:
`docs/sleeper/03_LEGACY_PRESERVATION.md` and `docs/sleeper/05_VAULT_OFFSITE_SYNC.md`.

- **Local copy:** `~/ff-legacy-vault/seal-v1-2026-11-07/` (read-only). Check it any time with
  `python3 ~/ff-legacy-vault/seal-v1-2026-11-07/verify.py`.
- **Off-machine copies** (age-encrypted, same bytes everywhere): Google Drive
  `ff-legacy-vault/`, iCloud Drive `ff-legacy-vault/`, and a release on the private
  `ocmooch/ff-legacy-vault` repo. seal-v1 is pinned forever; up to 4 newer archives are kept.
- **Tamper evidence:** the seal's `MANIFEST.json` is committed at
  `docs/sleeper/seals/seal-v1.manifest.json` (hashes and counts only; sha256
  `d11c9ff0b39e032e093cccab958224cdce97a9ea1ab4ec6d778599bba6178830`).
- **Sync:** `scripts/vault/vault_sync.sh` (no args = weekly snapshot + sync; also
  `pack-seal`, `snapshot`, `sync`, `status`). Per-destination status:
  `~/ff-legacy-vault/sync-status.json`; logs: `data/logs/vault_sync-<date>.log`.
- **Restore drill:** download one archive, then in a plain terminal (never through Claude)
  `scripts/vault/restore_check.sh primary <archive>` (passphrase) or `… recovery <archive>`
  (paste the paper key). Restore procedure: `03_LEGACY_PRESERVATION.md` §12.
- **Old one-off DB backups** live in `data/backups/historical/`; backup pruning never touches
  that folder.

### Legacy guard (live DB)

2010–2025 are sealed in the live DB (`seasons.is_sealed = 1`). SQLite triggers refuse any change
to their league facts from any writer — the pipeline, a script, or the `sqlite3` shell
(`legacy_guard: … use ff-pipeline legacy amend`). Stats, scoring, projections and nflverse data
keep refreshing on those years.

- `ff-pipeline legacy status` — current seal, sealed seasons, lock state, trigger count, last
  amendment.
- `ff-pipeline legacy verify` — full check against the current seal (triggers, sealed seasons,
  per-table and per-season hashes, row registry, `integrity_check`); `--quick` skips the
  row-by-row and integrity checks. Prints table, seasons and the changed / missing / added
  primary keys. Never repairs; exit 1 on mismatch.
- **Preflight:** `run`, `backfill`, `reconstruct`, `rescore` and `migrate up` run
  `legacy verify --quick` first and stop (exit 1) on a mismatch. If that happens, do not
  "fix forward": compare with the newest vault copy and restore or amend deliberately.
- **Changing sealed data** (the only way):
  `ff-pipeline legacy amend --reason "Correct 2019 wk7 trade (issue #NN)" -- uv run python scripts/fix_x.py --apply`.
  It requires a clean verify, writes `data/backups/amendments/fantasy-pre-amend-<slug>-<ts>.db`
  (never pruned), unlocks for 10 minutes (`--unlock-minutes`), runs the command, relocks even if
  it fails, records a `legacy_amendments` row and stores the next seal baseline (`seal-v2`, …).
  It then prints the vault follow-up: build and sync an archive for the new seal
  (`scripts/vault/build_vault_dir.py`, `scripts/vault/vault_sync.sh`; automated in S9).
- NFL.com is defunct and unreachable, so the sealed data cannot be re-crawled: the DB and the
  vault copies are the only record. The NFL.com commands (`reconstruct`, `reconstruct-owners`,
  `draft`, `avatars`, the NFL.com path of `run`/`backfill`) can no longer run, and `scoring load`
  into a sealed year is refused. Corrections are scripted fixes or overlays run through
  `legacy amend`, never a re-crawl.

## Sleeper league capture (2026+)

Raw Sleeper league payloads are archived before anything parses them (S2,
`docs/plans/S2-sleeper-league-capture.md`). Bytes: `SLEEPER_RAW_DIR`
(`data/raw/sleeper/<sha[:2]>/<sha>.json.gz`, gitignored). Index: the `source_payloads` table.

```
uv run ff-pipeline sleeper-league capture --season 2026   # all endpoints, weeks 1..current
uv run ff-pipeline sleeper-league capture --dry-run       # fetch + report, write nothing
uv run ff-pipeline sleeper-league lineage                 # season -> league id chain
uv run ff-pipeline sleeper-league archive --endpoint matchups --week 3   # list revisions
```

- Needs `SLEEPER_LEAGUE_ID` (the newest season's league). At a season rollover, set it to the
  new league id; earlier seasons are found through `previous_league_id`.
- Re-running is safe: identical responses only bump `last_fetched_at` / `fetch_count`;
  changed responses (stat corrections, late moves) become new revisions. `stats_week`
  revisions also appear when only Sleeper's embedded player news changed (expected, ~100 KB each).
- Exit code 1 = at least one request failed after retries (listed); everything else was archived.
  Re-run later. A 429 waits for `Retry-After` (max 120 s).
- It is a writing command, so the legacy preflight runs first.
- **Weekly job:** Windows task `dz-sleeper-capture` (Wed 06:00, runs at next start if missed)
  runs `scripts/sleeper/weekly_capture.sh` from the main copy (`~/danger-zone-main`, see "The main
  copy" below), log in `data/logs/sleeper_capture-<UTC date>.log`. Check it with
  `schtasks.exe /Query /TN dz-sleeper-capture /V /FO LIST` ("Last Result" 0 = ok; 3 = refused, not
  on `main`). Registered with:
  ```powershell
  $action  = New-ScheduledTaskAction -Execute "wsl.exe" `
             -Argument "-d Ubuntu -u mainuser -- /home/mainuser/danger-zone-main/scripts/sleeper/weekly_capture.sh"
  $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At 6am
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
  Register-ScheduledTask -TaskName "dz-sleeper-capture" -Action $action -Trigger $trigger -Settings $settings
  ```
- **Status snapshots (S7-D7):** the weekly capture's players directory (Wednesday 06:00 ET,
  after Monday night) becomes one `player_status_snapshots` row per league player at the next
  load: the status after that week's games. Each week's official game status (Out / Doubtful /
  Questionable) comes from nflverse: `uv run ff-pipeline injury-reports --season 2026`
  (`player_injury_reports`; part of the go-live sequence).
- A full capture also downloads the league / owner / team avatars into `ASSETS_DIR` (new URLs
  only, deduped by sha256); the load links them to `teams`.
- Probe / fixtures: `scripts/sleeper/probe_field_mapping.py` (evidence report, offline) and
  `scripts/sleeper/build_fixtures.py` (pseudonymized test fixtures; review the diff before commit).

## Sleeper identity (S4)

Owners, the 2026 placeholder and players, all from the archive (`docs/plans/S4-identity.md`).

```
uv run ff-pipeline owners map                  # validate data/owner_platform_identities.yaml (read-only)
uv run ff-pipeline owners map --apply          # load owner_platform_identities (idempotent)
uv run ff-pipeline sleeper-league resolve-players          # dry run: resolved / stub-needed
uv run ff-pipeline sleeper-league map-player <sleeper_id> <player_id> --note "why"
uv run ff-pipeline legacy quarantine-2026-stub # one-time (done 2026-11-09); re-run = no-op
```

- **New Sleeper user** (e.g. a new manager at a renewal): `owners map` fails and prints the YAML
  stanza to add. Use an existing `owner_id`, or `new_owner: true` + `owner_display_name` for a
  genuinely new person. Only `confidence: high` / `confirmed` is accepted. The YAML holds real
  names, stays local, and is gitignored.
- **stub-needed player:** first check whether the player already exists under another name or
  position (nickname, CB vs DB). If yes, pin it with `map-player`. Otherwise the S5 loader creates
  a stub (league-referenced players only).
- **The quarantine** wrote its own `legacy_amendments` row (`legacy amend` audits only sealed
  rows). The stub's rows are in `legacy_nfl_com_2026_preseason` and in the seal-v1 vault archive.
  A pre-move backup sits in `data/backups/amendments/`.

## Sleeper season load (S5)

```
uv run ff-pipeline sleeper-league load --dry-run   # load + roll back: counts and records check
uv run ff-pipeline sleeper-league load             # archive → seasons/teams/matchups/rosters/transactions
```

- Offline: reads only the raw archive. Capture first (`sleeper-league capture`).
- Idempotent: a reload prints `+0 ~0 -0` for unchanged data. A stat correction shows up as `~N`
  on that week's matchups and rosters.
- "records vs Sleeper" must say `ok`. A difference means a scored week changed after capture
  (re-capture, then reload) or a mapping bug (stop and investigate).
- "owner change NOT written": a sealed (legacy) owner would change `is_active`/`joined_year`/
  `left_year`, which needs `legacy amend`.
- Since S7 the load also writes `playoff_brackets`, postseason flags and places, season results
  (champion / runner-up / Sacko), `traded_picks`, team extras, `seasons.settings_snapshot` and
  status snapshots. A warning "bracket winner scored less than loser" means Sleeper's bracket
  may run loser-advances: places are withheld; re-probe the bracket (`04` §7) before trusting it.
- **Live use is gated (S5-D1):** load the live DB only once the dashboard is ready to show 2026
  (dz-dashboard D0–D3). Until then, use a copy.

## Sleeper scoring (S6)

Run after a season load, in this order (each is offline except the nflverse fetches):

```
uv run ff-pipeline sleeper-league adopt-gsis --season 2026          # list; then --apply
uv run ff-pipeline injury-reports --season 2026                     # weekly official game status
uv run ff-pipeline run --source nflverse --season 2026              # stats + 40+/ST play counts
uv run ff-pipeline sleeper-league rules --diff 2025                 # review; then --write
uv run ff-pipeline run --source team_defense --season 2026          # DST rows (Sleeper PA rule)
uv run ff-pipeline sleeper-league stats                             # Sleeper's own stats, non-primary
uv run ff-pipeline rescore --season 2026
uv run ff-pipeline sleeper-league reconcile --season 2026 --out docs/sleeper/reports/2026-reconcile.md
```

- `adopt-gsis`: Sleeper has no gsis id for most rookies, so a rostered rookie and the row nflverse
  creates for him are two rows. It lists the exact name+position pairs and moves the real id and
  stats onto the rostered row; anything ambiguous is listed and skipped (fix with `map-player`).
  Re-run it after each nflverse ingest while rookies keep debuting.
- `rules`: any non-zero Sleeper setting the mapping doesn't know aborts. A change to the league's
  settings is a new D-10 sign-off, not a silent rewrite.
- `reconcile` classes: `stat_source` = Sleeper's own stats reproduce its points, nflverse differs
  (expected, small); `rule` = they don't (stop: a mapping bug); `unexplained` = no Sleeper stats
  stored (run `sleeper-league stats`). The in-progress week is reported apart.
- **Live use** joins the S5 go-live: load → the sequence above (S6-D1).

## Sleeper era operations (S9)

**Every week (unattended):** `dz-sleeper-capture` (Wed 06:00 ET) runs
`scripts/sleeper/weekly_capture.sh`:
1. capture;
2. `sleeper-league update --if-enabled`;
3. full `legacy verify`.

The log is `data/logs/sleeper_capture-<UTC date>.log`. Each step prints `ok` or `FAILED`; the job
stops at the first failure. Fix the cause, then run the same thing by hand from the main copy
(`cd ~/danger-zone-main && uv run ff-pipeline sleeper-league update`). Nothing is lost by waiting
a week: every capture re-fetches the whole season.

**The main copy (`~/danger-zone-main`):** the weekly capture (Wed 06:00) and the vault sync
(Wed 07:00) run only on released code, never on `dev` or a
feature branch, whose unfinished work could change what a capture records. The main copy is a
git worktree of this repo pinned to `main`. It shares the live data by symlinks: `.env` and
`data/{fantasy.db, fantasy_validation.db, owner_platform_identities.yaml, assets, raw, reports,
logs, backups, nflverse_cache}` point into `~/danger-zone`. The tracked files in `data/` come from
`main`. A sparse checkout hides the three `.gitkeep` files under the linked directories, so
`git status` stays clean. Never commit or develop in it.

- **Missed runs (PC off or nobody logged in):** both tasks run once, soon after the next login,
  that day or days later. They don't wake the PC. Nothing is lost: each capture re-fetches the
  season to date, and a missed week only means one fewer rotating snapshot. When both are
  missed they start together. The vault run then waits (2-min grace, up to 30 min) for the
  capture's lock (`data/logs/weekly_capture.lock`), so its snapshot still holds that week's data.
  The wait is logged as `wait:` lines in `data/logs/vault_sync-<date>.log`. Moving these jobs to an
  always-on server with cron is a deferred item (`10_OPEN_QUESTIONS.md` "Sleeper era").
- **After each `dev → main` release:** follow the release checklist in `CONTRIBUTING.md`
  ("Releases"). Until the main copy is pulled, both jobs keep running the previous release.
- **Rebuild it** (e.g. after a WSL reinstall):
  ```bash
  cd ~/danger-zone && git worktree add --no-checkout ~/danger-zone-main main && cd ~/danger-zone-main
  git config --worktree sparse.expectFilesOutsideOfPatterns true
  git sparse-checkout set --no-cone '/*' '!/data/' '/data/*' '!/data/logs/' '!/data/backups/' '!/data/nflverse_cache/'
  git checkout main && ln -s ~/danger-zone/.env .env
  for p in fantasy.db fantasy_validation.db owner_platform_identities.yaml assets raw reports logs backups nflverse_cache; do
    ln -s ~/danger-zone/data/$p data/$p; done
  uv run ff-pipeline legacy verify     # must read the live DB and pass
  ```

**`sleeper-league update`** (the season update, also the go-live command):
- runs load → `adopt-gsis --apply` → `injury-reports` → nflverse → team_defense →
  `sleeper-league stats` → `rules --check` → rescore → reconcile (report in `data/reports/`);
- `--dry-run` stops after a dry-run load;
- a sealed season → "nothing to update";
- if the league's scoring settings changed, it reports `rules check: CHANGED` and exits 1
  **without** rescoring. Review with `sleeper-league rules --diff <prev year>`, get the user's
  sign-off, run `rules --write`, then re-run `update`.

**2026 go-live (once, with the user's approval, after dashboard D0–D3):**
1. `uv run ff-pipeline migrate up` (adds guard v2; inert until a Sleeper season is sealed);
2. `uv run ff-pipeline sleeper-league rules --diff 2025`, then `rules --write`;
3. `uv run ff-pipeline sleeper-league update`;
4. set `SLEEPER_AUTO_LOAD=true` in `.env`; from then on the weekly job keeps the season current.

**Rollover (a renewed league):** when `/state/nfl` moves to a new season, the capture looks up the
commissioner's leagues for that season. It archives the league whose `previous_league_id` is
the current one, and prints "renewed league found and archived: <id>". Then, with the user:
1. set `SLEEPER_LEAGUE_ID=<id>`;
2. map any new owners (`owners map`);
3. sign off the new season's rules (`rules --diff`);
4. run `update`.

Nothing loads the new season by itself.

**Season close (~mid-January, with the user):**

`uv run ff-pipeline legacy seal-season --season 2026 --champion "<team name as shown>"`

It refuses unless the league is complete, the final week is ≥ 7 days old, the reconcile has no
`rule` / `unexplained` divergence, the bracket check is clean and the typed champion matches.
It then:
1. backs up to `data/backups/historical/fantasy-pre-seal-2026-<ts>.db`;
2. seals 2026 as the next `seal-vN` (incl. guard v2 fingerprints of the Sleeper-era columns and
   brackets) and verifies;
3. writes `docs/sleeper/seals/seal-vN.manifest.json` (+ `.sha256`; hashes only, commit it);
4. runs `vault_sync.sh build-seal` → `pack-seal` → `sync`.

Every `seal-*` archive is kept forever. The `ff-legacy-evergreen` folders stay seal-v1 only and
are never touched. After the seal, the weekly `update` reports "2026 is sealed; nothing to
update". `--skip-vault` seals the DB only (then run the three vault steps by hand).

**Restore drills** (quarterly + before each release; log in `docs/sleeper/drills.md`):
- you: `scripts/vault/restore_check.sh primary <newest archive>`;
- key-free: `uv run python scripts/vault/local_restore_drill.py`.

## When to escalate to development work

The runbook covers recovery; the following push you back to writing code (and updating tests):

- A scoring rule comparison fails consistently for a single player across many weeks → engine bug, fix in `scoring/engine.py`.
- A parser fails on every page of a type → upstream HTML change, fix in `crawlers/nfl_com/parsers.py`.
- Backfill resumes correctly but loses data on a clean re-run → upsert bug, fix in `repository/upsert.py`.
- `cookie set` accepts an obviously broken cookie → validation bug in `crawlers/nfl_com/client.py::test_auth`.

For all of these: reproduce in a unit test before touching the code under fix. See [`07_TESTING_STRATEGY.md`](07_TESTING_STRATEGY.md) and [`CONTRIBUTING.md`](../CONTRIBUTING.md).
