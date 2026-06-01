# Fantasy Football Pipeline

Personal data aggregation pipeline for an NFL.com fantasy league. Pulls league data, NFL stats, and projections from multiple sources; normalizes them into a unified database; and exposes a read API for downstream dashboards and decision tools.

This is **Phase 1** of a three-phase project:
- **Phase 1** (this repo): data foundation
- **Phase 2**: analytics dashboard
- **Phase 3**: AI-assisted GM decision support

## Quick start

### Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- An active NFL.com fantasy league you own/co-manage (Phase 1 reads but never writes to NFL.com)
- A populated `.env` file (see `.env.example`)

### Install

```bash
git clone <this-repo> fantasy-football
cd fantasy-football
cp .env.example .env
# Edit .env — at minimum set NFL_LEAGUE_ID, NFL_COOKIE, LEAGUE_START_YEAR.
# To extract NFL_COOKIE: log into fantasy.nfl.com, open DevTools →
# Application → Cookies → copy the full Cookie header. Wrap in single quotes.
uv sync
```

### First run

```bash
# 1. Create database + run migrations (idempotent — safe to re-run)
uv run ff-pipeline init

# 2. Verify NFL.com cookie works
uv run ff-pipeline cookie test

# 3. Load this season's scoring rules (one-time per scoring change)
uv run ff-pipeline scoring load --csv path/to/league-settings.csv

# 4. Backfill historical seasons (resumable; ~1 hour for 10 seasons)
uv run ff-pipeline backfill

# 5. Start the read API
uv run ff-pipeline serve
```

Then open `http://127.0.0.1:8000/docs` for the interactive API explorer.

For day-2 operations (cookie expiry, database corruption, parse failures), see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Architecture

```
data sources → crawlers → normalizer → scoring engine → database → API
```

Data sources:
- **NFL.com fantasy HTML** — your league's rosters, matchups, transactions, scoring rules (cookie auth)
- **nflverse** (via `nflreadpy`) — authoritative NFL player stats and play-by-play
- **Sleeper API** — projections, trending players, supplementary metadata

See `docs/` for the full design package.

## Commands

```bash
ff-pipeline init                  # Set up DB + run migrations
ff-pipeline run                   # Full sync (current season)
ff-pipeline run --source nflverse # Sync only one source
ff-pipeline run --source nfl_com --snapshot-kind pre_kickoff  # Game-day snapshot
ff-pipeline backfill              # Pull historical seasons (resumable, idempotent)
ff-pipeline backfill --season 2020   # Single season
ff-pipeline rescore               # Recompute league points from raw stats
ff-pipeline status [--verbose]    # Show pipeline health (last run, source health, backups)
ff-pipeline cookie set            # Update NFL.com session cookie (validates before saving)
ff-pipeline cookie test           # Verify cookie validity
ff-pipeline verify --player NAME --season Y --week W  # Cross-check scoring against NFL.com
ff-pipeline verify --sweep --season Y                  # Sweep weeks 1/8/15
ff-pipeline scoring load --csv FILE  # Load scraped league scoring rules
ff-pipeline prune-players [--dry-run]  # Remove unrosterable IDP/OL players + fully-orphaned rows
ff-pipeline backup [--keep-days N]   # Snapshot SQLite DB to data/backups/
ff-pipeline serve [--reload]         # Start FastAPI read API
ff-pipeline migrate up | status      # Alembic helpers
ff-pipeline --help                # Full reference
```

Exit codes follow BSD `sysexits.h` — see `docs/08_OPERATIONS.md` § "When things break" for the full table. Most operationally important: `77` = cookie expired (run `ff-pipeline cookie set`), `4` = bad `.env`.

## Development

```bash
# Run all tests
uv run pytest

# Just unit tests (fast, no I/O)
uv run pytest tests/unit

# Lint + format
uv run ruff check
uv run ruff format

# Type-check
uv run mypy src/

# Pre-commit hooks
uv run pre-commit install
```

## Documentation

Full design docs under `docs/`:

- `01_SPEC.md` — functional + non-functional requirements
- `02_ARCHITECTURE.md` — system design + module structure
- `03_DATA_SOURCES.md` — what each source provides and why we chose it
- `04_DATA_MODEL.md` — database schema
- `05_SCORING_ENGINE.md` — how stats translate to fantasy points
- `06_API_CONTRACT.md` — REST API for Phase 2/3 to consume
- `07_TESTING_STRATEGY.md` — test layers and approach
- `08_OPERATIONS.md` — running, monitoring, recovery
- `09_ROADMAP.md` — implementation milestones
- `10_OPEN_QUESTIONS.md` — defaults and deferred decisions
- `RUNBOOK.md` — day-2 operational scenarios

Contributor guide: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Attribution

NFL stat data is provided under CC-BY 4.0 by the [nflverse](https://nflverse.nflverse.com/) project. FTN charting data within nflverse is CC-BY-SA 4.0. Player projection data is fetched from the [Sleeper](https://sleeper.com/) public API.

This software is for personal use only — it is not affiliated with the NFL, nflverse, or Sleeper.
