# 02 — Architecture

## High-level shape

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES (external)                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────────┐ │
│  │ NFL.com fantasy  │  │  nflverse data   │  │  Sleeper public API    │ │
│  │ HTML pages       │  │  releases (CSV)  │  │  (REST)                │ │
│  │ (cookie auth)    │  │  (CC-BY 4.0)     │  │  (no auth)             │ │
│  └────────┬─────────┘  └────────┬─────────┘  └───────────┬────────────┘ │
└───────────┼─────────────────────┼────────────────────────┼──────────────┘
            │                     │                        │
            ▼                     ▼                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          CRAWLERS (per-source)                           │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────────┐ │
│  │ NflComCrawler    │  │  NflverseCrawler │  │  SleeperCrawler        │ │
│  │ (httpx + bs4)    │  │  (nflreadpy)     │  │  (httpx)               │ │
│  └────────┬─────────┘  └────────┬─────────┘  └───────────┬────────────┘ │
└───────────┼─────────────────────┼────────────────────────┼──────────────┘
            │                     │                        │
            ▼                     ▼                        ▼
            ┌─────────────────────────────────────────────────┐
            │       NORMALIZER (deduplication & id mapping)   │
            │   - resolves player identity across sources     │
            │   - validates ranges, fixes types               │
            │   - handles conflicts (which source wins?)      │
            └────────────────────────┬────────────────────────┘
                                     ▼
            ┌─────────────────────────────────────────────────┐
            │              SCORING ENGINE                      │
            │   - reads scraped league scoring rules           │
            │   - applies rules to every raw stat line         │
            │   - writes league_points alongside raw stats     │
            └────────────────────────┬────────────────────────┘
                                     ▼
            ┌─────────────────────────────────────────────────┐
            │              REPOSITORY (SQLAlchemy 2.0)         │
            │   - schema migrations via alembic                │
            │   - upserts (idempotent writes)                  │
            │   - SQLite default, PostgreSQL-compatible        │
            └────────────────────────┬────────────────────────┘
                                     ▼
            ┌─────────────────────────────────────────────────┐
            │              FASTAPI READ API                    │
            │   - localhost:8000 by default                    │
            │   - consumed by Phase 2 / Phase 3                │
            └─────────────────────────────────────────────────┘
```

## Module boundaries

The repo is organized so each box above corresponds to a Python package with a single, well-defined responsibility. **The boundaries matter**: when NFL.com breaks something, the change should be isolated to `crawlers/nfl_com/`. When you want to support a new league platform, you add one new crawler module.

```
fantasy-football/
├── src/
│   └── ff_pipeline/
│       ├── __init__.py
│       ├── settings.py              # pydantic-settings: env vars, paths
│       ├── cli.py                   # Typer-based CLI (ff-pipeline ...)
│       ├── orchestrator.py          # top-level run() function
│       │
│       ├── crawlers/
│       │   ├── __init__.py
│       │   ├── base.py              # abstract Crawler interface
│       │   ├── nfl_com/
│       │   │   ├── __init__.py
│       │   │   ├── client.py        # auth, session, retry
│       │   │   ├── league.py        # league-level scrapers
│       │   │   ├── teams.py         # team/roster scrapers
│       │   │   ├── matchups.py      # weekly matchup scrapers
│       │   │   ├── transactions.py  # waivers/trades/drops
│       │   │   ├── parsers.py       # BeautifulSoup parsers
│       │   │   └── urls.py          # all URL templates in ONE place
│       │   ├── nflverse/
│       │   │   ├── __init__.py
│       │   │   └── client.py        # wraps nflreadpy calls
│       │   └── sleeper/
│       │       ├── __init__.py
│       │       ├── client.py        # httpx + rate limiter
│       │       └── endpoints.py
│       │
│       ├── normalizer/
│       │   ├── __init__.py
│       │   ├── player_ids.py        # cross-source identity resolution
│       │   ├── stats.py             # stat field normalization
│       │   └── conflicts.py         # which source wins when they disagree
│       │
│       ├── scoring/
│       │   ├── __init__.py
│       │   ├── rules.py             # ScoringRules dataclass
│       │   ├── engine.py            # apply_rules(stats, rules) -> points
│       │   └── verifiers.py         # cross-check against NFL.com totals
│       │
│       ├── repository/
│       │   ├── __init__.py
│       │   ├── models.py            # SQLAlchemy 2.0 models
│       │   ├── upsert.py            # idempotent write helpers
│       │   └── queries.py           # read query helpers
│       │
│       ├── api/
│       │   ├── __init__.py
│       │   ├── main.py              # FastAPI app
│       │   ├── routes/
│       │   │   ├── leagues.py
│       │   │   ├── teams.py
│       │   │   ├── players.py
│       │   │   └── matchups.py
│       │   └── schemas.py           # Pydantic response models
│       │
│       └── logging_config.py        # structlog setup
│
├── alembic/                         # database migrations
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── nfl_com_html/            # saved HTML snapshots
│   │   └── sample_data/             # expected normalized outputs
│   ├── unit/
│   │   ├── test_scoring_engine.py
│   │   ├── test_parsers.py
│   │   └── test_normalizer.py
│   └── integration/
│       ├── test_pipeline_smoke.py
│       └── test_api_endpoints.py
│
├── scripts/
│   ├── backfill.py                  # historical bulk load
│   └── cookie_refresh_check.py
│
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── docs/                            # the docs from this handoff
```

## Why this structure

- **Source isolation**: every external system is a swappable plug-in. Test a single source in isolation.
- **One-way data flow**: crawlers don't talk to each other; the normalizer is the only point where data from multiple sources meets.
- **Scoring is pure**: the engine takes (stats, rules) and produces points. No side effects, no I/O. Easy to test.
- **The repo layer is the only thing that touches the database.** Crawlers and the API never write SQL directly.

## Key design decisions

### Why `httpx` over `requests`

- Async-capable (useful for parallelizing Sleeper calls)
- Better default cookie handling and HTTP/2 support
- Modern, actively maintained

### Why BeautifulSoup over a JS-rendered scraper (Playwright/Selenium)

NFL.com's fantasy pages **server-render** the data we need. The HTML returned by a vanilla HTTP request, with the user's session cookie, contains the roster, scoreboard, transaction log, etc. as parseable tables. JS-rendered scraping is **substantially more brittle and slower**, and adds a 200MB+ browser dependency. Reserve Playwright for a fallback that only activates if a static-fetch parser fails.

Confirmed via the working open-source scrapers (`PeteTheHeat/FF-Scraping`, `CyberJrod/FF-Scraping-and-Visualization`) which use exactly this approach.

### Why `nflreadpy` (and not `nfl_data_py`)

`nfl_data_py` was **archived by nflverse on Sept 25, 2025**. Its successor `nflreadpy` is the actively maintained Python port of the R `nflreadr` package. It uses Polars instead of pandas, is faster, and gets nightly data updates pushed to `nflverse-data` GitHub releases.

Stat data from nflverse is:
- Licensed **CC-BY 4.0** (FTN's charting data is CC-BY-SA 4.0)
- Updated within 15 minutes of game end on game days
- Re-updated Tuesday/Wednesday night with NFL stat corrections
- A drop-in for the "external API" mentioned in the original charter — far better than scraping ESPN

### Why SQLite for now

For a single-user system with O(100k) total stat-line rows:
- Zero ops (no daemon, no auth, no setup)
- File-based — easy backup (just copy the `.db` file)
- Fast enough for everything Phase 2 will need
- SQLAlchemy 2.0 + alembic make the eventual swap to PostgreSQL a config change

The schema in `04_DATA_MODEL.md` uses **only** SQL features available in both SQLite and PostgreSQL.

### Why FastAPI

- Pydantic v2 integration is best-in-class
- Automatic OpenAPI docs — Phase 2/3 can read the schema directly
- Async-ready
- Smallest learning surface among Python web frameworks

### Why Typer for the CLI

- Same author as FastAPI, identical typing conventions
- Automatic `--help` generation
- Trivial subcommand structure (`ff-pipeline run`, `ff-pipeline backfill`, etc.)

---

## Data flow walkthrough — an in-season sync

When `ff-pipeline run` fires on a Tuesday morning during the season:

1. **Orchestrator** loads settings, opens a DB session, and creates a `pipeline_runs` record (status=`running`).
2. **NflverseCrawler** runs first. It calls `nflreadpy.load_player_stats([2025])` and `load_pbp([2025])`, downloading any updated files. New rows go to `player_stats_raw`.
3. **NflComCrawler** runs second. It:
   - Hits `fantasy.nfl.com/league/{LEAGUE_ID}` to confirm the cookie works.
   - Hits the current week's matchup page → parses each matchup's lineups → upserts to `matchups` and `roster_slots`.
   - Hits the league transactions page → diff against existing rows → insert any new transactions.
   - Hits the current scoreboard → confirms scores.
4. **SleeperCrawler** runs third. It calls `/projections/nfl/{year}/{week}` for projections and `/players/nfl/trending/add` for waiver-priority context.
5. **Normalizer** runs:
   - Joins NFL.com player IDs to nflverse `gsis_id`s using the `player_id_map` table (built once during initial backfill, updated incrementally)
   - Validates stat ranges (no negative passing yards, etc.)
   - Resolves conflicts (e.g., if Sleeper says a player is on Team X but nflverse says Team Y, prefer nflverse)
6. **ScoringEngine** runs:
   - Loads the league's `ScoringRules` (scraped from NFL.com once per season; cached)
   - For every (player, week) tuple in `player_stats_raw` that lacks a corresponding `player_stats_scored` row, computes points and inserts
7. **Pipeline run** completes — updates `pipeline_runs` row to `success` with summary stats.
8. **API** is read-only and never blocked by ingestion; queries always see a consistent snapshot via SQLAlchemy's transaction isolation.

---

## Failure modes and how each is handled

| Failure | Detection | Response |
|--------|-----------|----------|
| NFL.com cookie expired | HTTP 302 → login URL, or empty/auth-required page | Log clear error, exit non-zero, suggest `ff-pipeline cookie set` |
| NFL.com DOM changed | Parser returns None / KeyError | Catch in parser, log structured event, skip row, increment `parse_failures` counter |
| Network blip | httpx raises TimeoutException | Retry 3× with exp backoff |
| nflverse release not yet posted | 404 on the asset | Skip this source for this run; don't block others |
| Sleeper rate-limited | HTTP 429 | Honor `Retry-After`, back off, log |
| Database locked (concurrent run) | sqlite3.OperationalError | Acquire a file lock at orchestrator level — only one run at a time |
| Stat correction after initial ingestion | nflverse pushes new file with same key | Upsert by primary key — new values replace old, scoring auto-recomputes |

The "graceful degradation" principle: **a failing source produces an error log and a degraded run, not a crash.** A run that successfully updated stats and projections but failed to fetch NFL.com is still useful and is recorded as `partial_success`.
