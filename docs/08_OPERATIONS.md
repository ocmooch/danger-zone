# 08 — Operations

How to run, monitor, and recover the Phase 1 pipeline. This is the day-to-day playbook.

## Secrets management

All sensitive values live in a `.env` file at the project root. The file is gitignored. The application loads it via `pydantic-settings`.

### `.env` contents

```bash
# REQUIRED — your league
NFL_LEAGUE_ID=1234567
NFL_COOKIE='s_ecid=MCMID%7C12345...; nflnext-token=eyJ0eXAi...; ...'
LEAGUE_START_YEAR=2014

# OPTIONAL — database
DATABASE_URL=sqlite:///./data/fantasy.db
# For PostgreSQL: DATABASE_URL=postgresql+psycopg://user:pass@host/db

# OPTIONAL — API server
API_HOST=127.0.0.1
API_PORT=8000

# OPTIONAL — logging
LOG_LEVEL=INFO
LOG_FORMAT=json   # 'json' for structured, 'console' for dev
LOG_DIR=./data/logs

# OPTIONAL — rate limiting
NFL_COM_DELAY_SECONDS=2.0
SLEEPER_REQUESTS_PER_MIN=120

# OPTIONAL — cache
NFLVERSE_CACHE_DIR=./data/nflverse_cache

# OPTIONAL — tuning
SCORING_VERIFY_TOLERANCE=0.1   # points tolerance for `ff-pipeline verify`
SAVE_RAW_HTML=false            # forensic debugging — saves NFL.com responses to data/raw_pages/
```

See `.env.example` for the canonical, fully-commented template.

### Cookie refresh workflow

When the cookie expires (auth failure detected during a run):

1. Pipeline run exits with code **77** (`EX_NOPERM`) — auth failure
2. Log writes: `"NFL.com authentication failed — refresh NFL_COOKIE via cookie set."`
3. User logs into NFL.com in browser and captures a fresh cookie (open DevTools → Application → Cookies → copy the value of the session cookie for `fantasy.nfl.com`)
4. User runs:
   ```bash
   ff-pipeline cookie set                  # prompts (hidden input)
   ff-pipeline cookie set --stdin < cookie.txt   # non-TTY / scripted use
   ```
   This validates the cookie via a single test request to NFL.com and writes it to `.env` **only if validation passes**
5. Re-run `ff-pipeline run`

The cookie validation step prevents the easy mistake of pasting a broken cookie and having the next 47 cron runs fail silently.

## CLI surface

The single entry point is `ff-pipeline`, implemented with Typer. Subcommands:

```bash
ff-pipeline init                  # Create database, run migrations, verify config
ff-pipeline run                   # Full sync from all sources
ff-pipeline run --source nflverse # Sync only one source
ff-pipeline run --verify          # Also run data-quality checks at end
ff-pipeline run --dry-run         # Show what would happen, don't write

ff-pipeline backfill              # Backfill historical seasons (resumable)
ff-pipeline backfill --start 2018 # From a specific year
ff-pipeline backfill --season 2020 # Single season only

ff-pipeline reconstruct           # Rebuild real history from NFL.com /history pages (resumable)
ff-pipeline reconstruct --start 2010 --end 2025  # Explicit range (--end defaults to current year-1)
ff-pipeline reconstruct --season 2018 --force    # Redo one already-completed season

ff-pipeline rescore               # Recompute scoring from raw stats
ff-pipeline rescore --season 2024 # For one season
ff-pipeline rescore --dry-run     # Report diffs, don't write

ff-pipeline status                # Show pipeline health, last run, per-source status
ff-pipeline status --verbose      # Include recent errors

ff-pipeline cookie set            # Refresh NFL.com cookie (interactive prompt)
ff-pipeline cookie test           # Verify the current cookie works

ff-pipeline verify --player NAME --season Y --week W   # Compare our score vs NFL.com
ff-pipeline verify --sweep --season Y                  # Sweep weeks 1/8/15 for a season

ff-pipeline scoring load --csv path/to/settings.csv    # Load scraped scoring rules

ff-pipeline serve                 # Start the FastAPI server
ff-pipeline serve --reload        # Dev mode with auto-reload

ff-pipeline backup                # Snapshot data/fantasy.db -> data/backups/
ff-pipeline backup --keep-days 60 # Override the 30-day pruning window

ff-pipeline migrate up            # Run pending alembic migrations
ff-pipeline migrate down --rev N  # Rollback to a revision (stub for M11+)
ff-pipeline migrate status        # Show current migration state

ff-pipeline export --table NAME --format csv   # Dump a table for ad-hoc analysis (stub)
```

## Scheduling

Phase 1 default: **cron on your local machine.** A ready-to-install crontab ships at `scripts/cron.example` — placeholders `<PROJECT_ROOT>` and `<FF_PIPELINE>` are the only edits required:

```bash
# Find the binary path:
which ff-pipeline                          # if installed via `uv tool install .`
# or: ls $PWD/.venv/bin/ff-pipeline        # for a uv-managed venv

sed -i 's|<PROJECT_ROOT>|/abs/path/to/checkout|g; s|<FF_PIPELINE>|/abs/path/to/ff-pipeline|g' \
    scripts/cron.example

crontab scripts/cron.example               # install (replaces existing crontab)
crontab -l                                  # verify
```

The shipped schedule covers four in-season runs (Sun 23:30, Mon 23:30, Tue 09:00, Wed 23:30 + `--verify`) plus a nightly 04:00 SQLite backup. Times are local. The pipeline is idempotent so over-running is safe.

Off-season (February → early September): keep the same schedule or trim to a weekly Sunday run.

### Why not a daemon / scheduled task / launchd?

For a single-user system, cron is the lowest-overhead approach. If/when you go cloud-hosted (a Phase 2 question), the same `ff-pipeline run` command works under any modern scheduler (systemd timers, GitHub Actions schedule, Fly.io scheduled tasks, etc.).

### Keeping the read API up

The data sync runs on cron, but the read API (`ff-pipeline serve`, criterion ②) is a long-lived process. A bare `nohup ff-pipeline serve &` works for an ad-hoc check but dies with its parent shell and does not come back after a reboot. The durable Phase-1 answer is a **systemd user service** — this box runs `systemd` as PID 1 and `systemctl --user` is available.

A ready-to-install unit ships at `scripts/ff-pipeline-api.service` (same placeholder convention as `cron.example`):

```bash
mkdir -p ~/.config/systemd/user
cp scripts/ff-pipeline-api.service ~/.config/systemd/user/
# edit <PROJECT_ROOT> / <FF_PIPELINE> in the copied file first
systemctl --user daemon-reload
systemctl --user enable --now ff-pipeline-api.service
sudo loginctl enable-linger "$USER"   # survive logout + reboot (WSL2/headless)
curl -s http://127.0.0.1:8000/health
```

It restarts on crash (`Restart=on-failure`) and on reboot (`WantedBy=default.target` + linger). Logs go to `journalctl --user -u ff-pipeline-api.service`. Host/port come from `API_HOST`/`API_PORT` in `.env`.

## Logging

- Structured JSON via `structlog`
- Written to `data/logs/pipeline.log` with rotation (daily, keep 14 days)
- Each log line includes: `timestamp`, `level`, `event`, `pipeline_run_id`, `source` (where relevant), plus event-specific fields
- The NFL_COOKIE value is **never** logged — `structlog` processor scrubs it

### Sample log line

```json
{
  "timestamp": "2025-11-19T08:00:23.412Z",
  "level": "info",
  "event": "nfl_com.fetch_complete",
  "pipeline_run_id": 142,
  "source": "nfl_com_league",
  "url": "https://fantasy.nfl.com/league/1234567/history/2024/schedule",
  "status_code": 200,
  "duration_ms": 1247,
  "bytes": 84321
}
```

### Querying logs

`ff-pipeline status` (plain text) and `ff-pipeline status --verbose` (adds recent failures) are the fastest way to read state. The raw JSON log file lives at `data/logs/pipeline.log` and is rotated daily; older days have a `.YYYY-MM-DD` suffix.

```bash
# All NFL.com auth failures this season
cat data/logs/pipeline.log* | jq 'select(.event == "nfl_com.auth_failure")'

# Performance of each source
cat data/logs/pipeline.log | jq 'select(.event | endswith("fetch_complete")) | {source, duration_ms}'
```

## Backups

The database is everything. Back it up.

### Manual backup

```bash
ff-pipeline backup                  # writes data/backups/fantasy-YYYY-MM-DD.db, prunes > 30d
ff-pipeline backup --keep-days 0    # keep all
ff-pipeline backup --backup-dir /mnt/external/ff-backups
```

Uses SQLite's online `.backup` API (safe while the DB is open). Logged + reported via `ff-pipeline status` (last backup is part of the standard output).

### Automated backup

`scripts/cron.example` already includes a nightly `ff-pipeline backup` at 04:00 with the default 30-day retention. No separate setup is needed once you've installed the example crontab.

### Cloud backup (optional)

If you want offsite backups, add an rclone or rsync step. The DB file is small (estimated < 200 MB even with 15+ years of history). For example:
```bash
# Sync to Backblaze B2 / S3 / Dropbox
rclone copy data/backups/ remote:fantasy-football-backups/
```

## Recovery scenarios

### "I think the data is wrong for week 5"

1. Inspect the raw row:
   ```sql
   SELECT * FROM player_stats_raw WHERE player_id = ? AND week = 5;
   ```
2. Cross-check against NFL.com via the verifier:
   ```bash
   ff-pipeline verify --player "Lamar Jackson" --season 2025 --week 5
   ```
3. If raw is wrong: `ff-pipeline run --source nflverse --season 2025` (idempotent — upserts overwrite stale rows)
4. If raw is right but score is wrong: scoring rule bug — re-run `ff-pipeline rescore --season 2025 --dry-run` to see the diff, then `ff-pipeline scoring load --csv ...` to re-load rules if they were wrong

### "The database file got corrupted"

1. Restore latest backup: `cp data/backups/fantasy-YYYY-MM-DD.db data/fantasy.db`
2. Run `ff-pipeline run` to catch up to current
3. Investigate root cause (file system issue? concurrent write?)

### "I want to start over"

```bash
# Nuclear option — destroys all data
rm data/fantasy.db data/nflverse_cache/*

ff-pipeline init
ff-pipeline backfill --start 2014   # rebuild from scratch
```

This takes ~1 hour for a 10+ year league.

### "NFL.com changed their HTML and the scraper is broken"

1. Pipeline run logs `nfl_com.parse_failure` with the URL and the saved-to-disk path for the failing HTML
2. Open the saved HTML, identify what changed
3. Update the parser in `crawlers/nfl_com/parsers.py`
4. Save the updated HTML as a new test fixture in `tests/fixtures/nfl_com_html/`
5. Update the corresponding unit test to use the new fixture
6. Run `pytest tests/unit/test_parsers.py`
7. When green, re-run the pipeline

## Performance expectations

| Operation | Expected time |
|-----------|---------------|
| Fresh full backfill (10 seasons) | 45-75 min (mostly NFL.com rate limit) |
| Incremental run during season | 2-5 min |
| Rescore one season | 5-10 sec |
| API query (any endpoint) | < 50 ms typical |
| Database file size | < 200 MB at 15 seasons |

If actual numbers diverge significantly: file a perf investigation issue.

## When things break, signal sequence

```
Pipeline run starts
  │
  ├─ Settings invalid ──────→ Exit 4,  log SettingsError detail
  │
  ├─ Cookie test fails ─────→ Exit 77, log "refresh NFL_COOKIE via cookie set"
  │
  ├─ A source fails ─────────→ Continue with others; status=partial_success
  │   └─ All sources fail ───→ Exit 1, log error summary
  │
  ├─ Parse failure ──────────→ Log structured error, save HTML when SAVE_RAW_HTML=true, skip row, continue
  │
  ├─ Data quality issue ─────→ Insert into data_quality_issues, log warning, continue
  │
  └─ All sources succeed ────→ status=success, exit 0
```

Exit codes (BSD `sysexits.h` values where applicable):
- `0` — success
- `1` — general failure (mid-backfill abort, verify mismatches, generic error)
- `2` — Typer / argparse usage error (bad flag combination)
- `4` — invalid configuration (`SettingsError`: missing `.env` value, bad value)
- `64` — stub command not implemented yet (`EX_USAGE`)
- `65` — bad input data (`EX_DATAERR`: scoring CSV parse failure, empty cookie input)
- `69` — service unreachable (`EX_UNAVAILABLE`: NFL.com network error during `cookie test`/`set`)
- `77` — authentication failure (`EX_NOPERM`: most common after cookie expiry; backfill remaps to this when the abort cause is `AuthFailureError`)
