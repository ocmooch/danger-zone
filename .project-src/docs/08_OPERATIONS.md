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

# OPTIONAL — rate limiting
NFL_COM_DELAY_SECONDS=2.0
SLEEPER_REQUESTS_PER_MIN=120

# OPTIONAL — cache
NFLVERSE_CACHE_DIR=./data/nflverse_cache
```

### Cookie refresh workflow

When the cookie expires (auth failure detected during a run):

1. Pipeline run fails with exit code 2 (auth failure)
2. Log writes: `"NFL.com authentication failed — refresh cookie. Run: ff-pipeline cookie set"`
3. User logs into NFL.com in browser, captures fresh cookie (per `prerequisites.md` step 2)
4. User runs:
   ```bash
   ff-pipeline cookie set
   ```
   This prompts for the cookie string (hidden input, like a password), validates it by making a single test request to NFL.com, and writes it to `.env` only if validation passes
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

ff-pipeline rescore               # Recompute scoring from raw stats
ff-pipeline rescore --season 2024 # For one season
ff-pipeline rescore --dry-run     # Report diffs, don't write

ff-pipeline status                # Show pipeline health, last run, per-source status
ff-pipeline status --verbose      # Include recent errors

ff-pipeline cookie set            # Refresh NFL.com cookie (interactive prompt)
ff-pipeline cookie test           # Verify the current cookie works

ff-pipeline verify --player NAME --season Y --week W   # Compare our score vs NFL.com

ff-pipeline serve                 # Start the FastAPI server
ff-pipeline serve --reload        # Dev mode with auto-reload

ff-pipeline migrate up            # Run pending alembic migrations
ff-pipeline migrate down --rev N  # Rollback to a revision
ff-pipeline migrate status        # Show current migration state

ff-pipeline export --table NAME --format csv   # Dump a table for ad-hoc analysis
```

## Scheduling

Phase 1 default: **cron on your local machine.** During the NFL regular season + playoffs (early September through early February):

```cron
# /etc/cron.d/ff-pipeline OR `crontab -e`
# Times are local time. Adjust to your zone.

# Sunday late evening sync — captures most game results
30 23 * * 0  cd /Users/you/code/fantasy-football && /Users/you/.local/bin/ff-pipeline run >> data/logs/cron.log 2>&1

# Monday late evening — after MNF
30 23 * * 1  cd /Users/you/code/fantasy-football && /Users/you/.local/bin/ff-pipeline run >> data/logs/cron.log 2>&1

# Tuesday morning — quick sync after MNF stat updates
0 9 * * 2    cd /Users/you/code/fantasy-football && /Users/you/.local/bin/ff-pipeline run >> data/logs/cron.log 2>&1

# Wednesday night — final sync after nflverse pushes corrections
30 23 * * 3  cd /Users/you/code/fantasy-football && /Users/you/.local/bin/ff-pipeline run --verify >> data/logs/cron.log 2>&1
```

Off-season (February → early September): weekly Sunday run is fine.

### Why not a daemon / scheduled task / launchd?

For a single-user system, cron is the lowest-overhead approach. If/when you go cloud-hosted (a Phase 2 question), the same `ff-pipeline run` command works under any modern scheduler (systemd timers, GitHub Actions schedule, Fly.io scheduled tasks, etc.).

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

```bash
# Errors from the most recent run
ff-pipeline status --verbose | jq '.errors'

# All NFL.com auth failures this season
cat data/logs/pipeline.log* | jq 'select(.event == "nfl_com.auth_failure")'

# Performance of each source
cat data/logs/pipeline.log | jq 'select(.event | endswith("fetch_complete")) | {source, duration_ms}'
```

## Backups

The database is everything. Back it up.

### Manual backup

```bash
sqlite3 data/fantasy.db ".backup data/backups/fantasy-$(date +%Y-%m-%d).db"
```

This is safe to run while the pipeline isn't actively writing.

### Automated backup

Add to cron:
```cron
# Backup database every day at 4 AM
0 4 * * *  cd /Users/you/code/fantasy-football && sqlite3 data/fantasy.db ".backup data/backups/fantasy-$(date +\%Y-\%m-\%d).db" && find data/backups -name "fantasy-*.db" -mtime +30 -delete
```

Keeps 30 days of backups.

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
2. Verify against NFL.com manually
3. If raw is wrong: `ff-pipeline run --source nflverse --force-refetch --season 2025 --week 5`
4. If raw is right but score is wrong: scoring rule bug — investigate

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
  ├─ Cookie test fails ─────→ Exit 2, log "Refresh cookie"
  │
  ├─ A source fails ─────────→ Continue with others; status=partial_success
  │   └─ All sources fail ───→ Exit 1, log error summary
  │
  ├─ Parse failure ──────────→ Save HTML, log structured error, skip row, continue
  │
  ├─ Database locked ────────→ Exit 3, log "Another run in progress"
  │
  ├─ Data quality issue ─────→ Insert into data_quality_issues, log warning, continue
  │
  └─ All sources succeed ────→ status=success, exit 0
```

Exit codes:
- `0` — success
- `1` — general failure
- `2` — authentication failure (most common after cookie expiry)
- `3` — concurrent run / locked database
- `4` — invalid configuration
