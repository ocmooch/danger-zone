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
- A populated `.env` file (see `.env.example`)
- See `docs/prerequisites.md` for what you need to set up before first run

### Install

```bash
git clone <this-repo>
cd fantasy-football
cp .env.example .env
# Edit .env — at minimum set NFL_LEAGUE_ID, NFL_COOKIE, LEAGUE_START_YEAR
uv sync
```

### First run

```bash
# Create database + run migrations
uv run ff-pipeline init

# Verify NFL.com cookie works
uv run ff-pipeline cookie test

# Backfill historical seasons (takes ~1 hour for 10 seasons)
uv run ff-pipeline backfill

# Start the read API
uv run ff-pipeline serve
```

Then open `http://127.0.0.1:8000/docs` for the interactive API explorer.

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
ff-pipeline run                   # Full sync from all sources
ff-pipeline run --source nflverse # Sync only one source
ff-pipeline backfill              # Pull historical seasons (resumable)
ff-pipeline rescore               # Recompute league points from raw stats
ff-pipeline status                # Show pipeline health
ff-pipeline cookie set            # Update NFL.com session cookie
ff-pipeline cookie test           # Verify cookie validity
ff-pipeline verify --player ... --season Y --week W   # Cross-check scoring
ff-pipeline serve                 # Start FastAPI read API
ff-pipeline --help                # Full reference
```

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

## Attribution

NFL stat data is provided under CC-BY 4.0 by the [nflverse](https://nflverse.nflverse.com/) project. FTN charting data within nflverse is CC-BY-SA 4.0. Player projection data is fetched from the [Sleeper](https://sleeper.com/) public API.

This software is for personal use only — it is not affiliated with the NFL, nflverse, or Sleeper.
