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

## When to escalate to development work

The runbook covers recovery; the following push you back to writing code (and updating tests):

- A scoring rule comparison fails consistently for a single player across many weeks → engine bug, fix in `scoring/engine.py`.
- A parser fails on every page of a type → upstream HTML change, fix in `crawlers/nfl_com/parsers.py`.
- Backfill resumes correctly but loses data on a clean re-run → upsert bug, fix in `repository/upsert.py`.
- `cookie set` accepts an obviously broken cookie → validation bug in `crawlers/nfl_com/client.py::test_auth`.

For all of these: reproduce in a unit test before touching the code under fix. See [`07_TESTING_STRATEGY.md`](07_TESTING_STRATEGY.md) and [`CONTRIBUTING.md`](../CONTRIBUTING.md).
