# 01 — Specification

## Purpose

Phase 1 builds the **data foundation** for the entire fantasy football management system. Its job is to make every piece of league, team, player, and scoring data available in a single, queryable, accurate, up-to-date store. Phases 2 (dashboard) and 3 (decision support) read from this store; they never touch the source systems directly.

## Functional requirements

### F1. League data ingestion

The system must ingest from the user's NFL.com fantasy league:
- **F1.1** League metadata: name, season year, scoring rules, roster configuration, owner list
- **F1.2** All teams in every season the user has access to, including team name, owner, draft slot, final standing
- **F1.3** Every roster move for every team: drafts, waivers, free-agent adds, drops, trades, and IR placements (with timestamps)
- **F1.4** Every weekly matchup: who played whom, lineups for each team, points scored per player slot
- **F1.5** Final season standings, playoff bracket results, and championship outcomes

### F1A. Game-time state (point-in-time snapshots)

For every (player, season, week), the system must capture state **as it was at game time** (i.e., immediately before the player's NFL game kicked off — Sunday 1:00 PM ET for most players, with Thursday/Saturday/Sunday-night/Monday-night variants):
- **F1A.1** **Waiver status**: `OWNED` (and by which team), `FREE_AGENT`, `ON_WAIVERS` (and claim deadline if applicable)
- **F1A.2** **Roster slot**: `STARTER` (which position — QB, RB1, FLEX, etc.), `BENCH`, `IR`, `NOT_ROSTERED`
- **F1A.3** **Date added / dropped from team**: the most recent transaction timestamp affecting the player's relationship with each team
- **F1A.4** **Lock state**: whether the player was locked at game time (NFL.com locks the lineup at kickoff)

This is a **time-series of state**, not just static facts. The data model uses snapshot tables so we can answer questions like "Who was on Team X's bench in Week 5 of 2022?" or "Was this player a free agent at the start of Week 8?"

### F2. Player data ingestion

The system must maintain a comprehensive NFL player roster:
- **F2.1** Every player who has been on any roster in the league's history must exist in the player table
- **F2.2** For each player: name, position, NFL team, status (active/IR/PUP/retired/etc.), birth date, years of experience, cross-platform IDs (NFL ID, Sleeper ID, ESPN ID, GSIS ID where available)
- **F2.3** Player updates run weekly during the season to capture trades, position changes, and roster moves

### F3. Statistical data ingestion

For every NFL player and every week of every relevant season:
- **F3.1** Raw weekly stats (passing yards, completions, attempts, TDs, INTs, rushing yards, receptions, targets, etc.) sourced from `nflverse` (canonical, nightly-updated)
- **F3.2** Defensive/ST stats per NFL team per week
- **F3.3** Kicking stats per player per week with distance bucket breakdown
- **F3.4** Projection data per player per week sourced from Sleeper
- **F3.5** Snap counts, opportunity stats (targets, carries, redzone touches) where available from nflverse

### F4. Scoring engine

- **F4.1** Translate every raw stat line into league-adjusted fantasy points using the **scraped scoring rules** from F1.1
- **F4.2** Store both the raw stat AND the calculated league points — never lose the underlying data
- **F4.3** Recompute league points on demand (when scoring rules change, or when nflverse pushes stat corrections)
- **F4.4** Verify scraped scoring rules against a hand-coded reference: pick three known historical weeks, compute points, compare to NFL.com's stored result. Discrepancy > 0.1 points is a failed verification.

### F5. Storage & query

- **F5.1** All data lives in a single SQLite database file by default (with migration path to PostgreSQL documented)
- **F5.2** The schema is versioned via `alembic`; migrations run automatically on app start
- **F5.3** A FastAPI service exposes read endpoints (no writes from outside the pipeline)
- **F5.4** Endpoints support filtering by season, week, team, player, and position

### F6. Scheduled execution

- **F6.1** A single `ff-pipeline run` command does a full sync: pulls all sources, normalizes, scores, persists
- **F6.2** During the NFL season (September–February), the pipeline runs automatically:
  - **Tuesday morning** (after MNF, including stat corrections): full sync
  - **Wednesday evening**: re-sync to catch the late stat corrections nflverse applies Mon–Wed
  - **Sunday late evening + Monday late evening**: lightweight sync to refresh scores
- **F6.3** Off-season: weekly sync to capture roster moves, suspensions, retirements
- **F6.4** Scheduling uses **cron on local machines** (Phase 1 default); the pipeline is idempotent so over-running is safe

### F7. Backfill

- **F7.1** A one-shot `ff-pipeline backfill --start 2014` (or whatever start year) command pulls every historical season
- **F7.2** Backfill is **resumable** — if it fails mid-way, rerunning it picks up where it left off
- **F7.3** Backfill is **idempotent** — rerunning produces the same database state

### F8. Authentication & secrets

- **F8.1** The NFL.com session cookie lives in `.env` (gitignored) and is loaded via `pydantic-settings`
- **F8.2** When auth fails (login redirect or 401), the pipeline writes a clear error log and exits non-zero
- **F8.3** Cookie refresh is a single command: `ff-pipeline cookie set`

---

## Non-functional requirements

### N1. Reliability

- **N1.1** Each source crawler retries on transient failures (HTTP 5xx, timeouts) with exponential backoff (3 attempts, base 2s)
- **N1.2** A failure in one source (e.g., NFL.com down) must not block ingestion from other sources
- **N1.3** All scrapers gracefully handle missing/changed DOM elements by logging a structured error and skipping the affected record (not crashing)

### N2. Observability

- **N2.1** Structured JSON logs via `structlog` (so future Phase 2/3 can query log history)
- **N2.2** Every pipeline run writes a summary record to a `pipeline_runs` table: timestamp, sources hit, rows added/updated, errors, duration
- **N2.3** A `ff-pipeline status` command shows last successful run per source and any unresolved errors

### N3. Testability

- **N3.1** Scoring engine has ≥95% line coverage with unit tests on every scoring rule type
- **N3.2** Each scraper has **fixture-based tests** — saved HTML snapshots of real NFL.com pages — that run offline (so DOM-change failures don't break unrelated tests)
- **N3.3** A "smoke test" runs end-to-end against a known historical week and verifies row counts match expected values

### N4. Performance (loose targets — single user)

- **N4.1** Full backfill of 10 seasons completes in under 1 hour
- **N4.2** An in-season incremental sync completes in under 5 minutes
- **N4.3** API endpoints return in under 500ms for 99% of requests against an indexed database

### N5. Maintainability

- **N5.1** No source-specific code outside the source's dedicated module (`crawlers/nfl_com/`, `crawlers/nflverse/`, `crawlers/sleeper/`)
- **N5.2** All cross-source joins go through the **normalizer** layer with explicit ID mapping
- **N5.3** When NFL.com changes its DOM, only files in `crawlers/nfl_com/` should need editing

### N6. Forward compatibility

- **N6.1** Schema accommodates **any** league scoring system, not just the user's current one (i.e., scoring rules are data, not code)
- **N6.2** Player ID table includes mapping columns for ESPN, Yahoo, and Sleeper IDs from day one, so future platform migrations don't require schema changes
- **N6.3** The crawler interface is abstract — adding a new source (Yahoo, ESPN, Sleeper league sync) means implementing a single Python class
- **N6.4** **Schema is extensible by design.** New sections can be added in two ways without breaking existing data:
  - **New table**: register a new model + alembic migration; existing queries unaffected
  - **New optional column**: nullable additions via migration; existing rows get NULL defaults
  - Every entity table includes an `extra_data` JSONB-style column (TEXT JSON in SQLite) for opportunistic capture of fields we haven't yet promoted to first-class columns — so when we discover something new on a scraped page, we can store it immediately and formalize the column later

### N7. Legal & responsible scraping

- **N7.1** Crawlers respect `robots.txt` where it applies to the URLs being fetched
- **N7.2** Rate-limit: NFL.com fetches throttle to **1 request per 2 seconds**; Sleeper to **≤ 1000/min** (their published limit)
- **N7.3** A descriptive `User-Agent` identifies the crawler (e.g., `fantasy-pipeline/0.1 (personal-use)`)
- **N7.4** Data is for personal use only — the package never re-publishes scraped league data publicly

### N8. Security

- **N8.1** Cookie and any other secrets live only in `.env`, which is gitignored
- **N8.2** Logs **never** print the cookie value (replace with `[REDACTED]`)
- **N8.3** API endpoints bind to `127.0.0.1` only by default — no internet exposure without explicit config change

---

## Explicit non-goals for Phase 1

To prevent scope creep:

- ❌ **No real-time / in-game stat updates.** Weekly granularity is sufficient.
- ❌ **No write operations to NFL.com.** Setting lineups, making trades, etc. stays in the NFL.com UI. Phase 1 is read-only.
- ❌ **No analysis or predictions.** Those are Phase 3.
- ❌ **No UI.** That's Phase 2.
- ❌ **No multi-league support.** Single league; multi-league can be added later as the schema already supports it.
- ❌ **No DFS data, betting lines, prop bets.** Out of scope.
