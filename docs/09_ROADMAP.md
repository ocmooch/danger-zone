# 09 — Roadmap

A milestone-by-milestone implementation order. Each milestone is a coherent unit of work — a Claude Code session, roughly. The order is designed so each milestone produces something testable, and so dependencies flow in one direction (later milestones can use earlier ones; never the reverse).

The order is **not negotiable** without thinking hard. Two specific reasons:
- Scoring engine before any crawler (M3 before M4-6): so you have a tested correctness oracle before depending on data
- API before automation (M8 before M9): so you can manually validate data before scheduling

## Milestone summary

| # | Milestone | Time est. | Deliverable |
|---|-----------|-----------|-------------|
| M0 | Project bootstrap | 30 min | Repo, env, deps installed, tests pass |
| M1 | Database schema + migrations | 1-2 hr | Empty DB with all tables + migrations |
| M2 | Settings + logging + CLI shell | 1 hr | `ff-pipeline --help` works; `.env` loads |
| M3 | Scoring engine (no I/O) | 2-3 hr | Unit tests cover every rule type |
| M4 | nflverse crawler | 2 hr | Player stats land in `player_stats_raw` |
| M5 | NFL.com crawler (current season only) | 4-6 hr | League, teams, rosters, matchups, transactions populated |
| M6 | Sleeper crawler | 1-2 hr | Projections, trending data populated |
| M7 | Normalizer + ID resolution | 3 hr | Players join across sources cleanly |
| M8 | FastAPI service | 2-3 hr | All endpoints in `06_API_CONTRACT.md` work |
| M9 | Backfill + verification | 2-3 hr | Historical seasons populated; scoring verified vs NFL.com |
| M10 | Scheduling + observability polish | 1-2 hr | Cron config; `ff-pipeline status` works; logs tidy |
| M11 | Documentation pass | 1 hr | README, runbook, examples |

**Total**: roughly 20-25 hours of focused work for someone working with Claude Code, more if exploring/researching as you go. Expect 1-2 weeks of part-time effort.

---

## M0 — Project bootstrap

**Goal**: a clean repo with all dependencies pinned and a passing `hello world` test.

**Tasks**:
- Clone or create the repo at `~/code/fantasy-football`
- Copy starter files from the handoff package
- Install `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Run `uv sync` (installs from `pyproject.toml`)
- Create `data/` directory structure: `data/{logs,backups,nflverse_cache}`
- Confirm `pytest tests/unit/test_smoke.py` passes (just verifies imports work)
- Initialize git, make first commit
- Set up pre-commit hooks: `pre-commit install`

**Done when**:
- `uv run ff-pipeline --version` prints a version
- `uv run pytest` exits 0 (even with no tests yet — one trivial test is fine)
- `.env` exists with your real values; `.env.example` is the template

---

## M1 — Database schema + migrations

**Goal**: All tables from `04_DATA_MODEL.md` exist; alembic manages them.

**Tasks**:
- Implement every model in `src/ff_pipeline/repository/models.py` using SQLAlchemy 2.0 typed syntax
- Set up alembic: `alembic init alembic`, configure `alembic/env.py` to import models
- Generate initial migration: `alembic revision --autogenerate -m "initial schema"`
- Inspect the generated migration carefully — autogen sometimes misses things
- Run `alembic upgrade head` against a fresh SQLite database
- Add `tests/integration/test_alembic_migrations.py` (up + down round trip)
- Wire `ff-pipeline init` and `ff-pipeline migrate` subcommands

**Done when**:
- `ff-pipeline init` creates `data/fantasy.db` with all tables
- `sqlite3 data/fantasy.db ".tables"` shows the full list
- Migrations round-trip test passes

---

## M2 — Settings, logging, CLI shell

**Goal**: configuration and observability foundation; commands exist (some are no-ops, that's fine).

**Tasks**:
- `settings.py`: pydantic-settings model loading `.env`, with type validation and clear errors for missing values
- `logging_config.py`: structlog setup, processors for JSON output, cookie redaction
- `cli.py`: Typer app with stub implementations of every subcommand in `08_OPERATIONS.md`
- Test: load real `.env`, verify settings parse; bad `.env` produces a clean error

**Done when**:
- `ff-pipeline --help` lists every subcommand
- Logs are JSON, redacted, and routed to file + stderr
- Settings model raises if `NFL_COOKIE` is missing

---

## M3 — Scoring engine

**Goal**: a tested, pure function that scores a stat dict given a rules object.

**Tasks**:
- `scoring/rules.py`: `ScoringRule` and `ScoringRules` dataclasses
- `scoring/engine.py`: the `apply_rules` function from `05_SCORING_ENGINE.md`
- `tests/unit/test_scoring_engine.py`: at least 30 tests covering:
  - Each stat key independently
  - Each bonus rule on/off behavior at thresholds
  - Composition (multi-key stat lines)
  - Negative stats
  - Missing keys default to zero
- Coverage report: aim for 95%+ on `scoring/engine.py`

**Done when**:
- All scoring unit tests pass
- Coverage report confirms ≥95% on the engine
- A REPL session like `apply_rules({"passing_yards": 312, "passing_tds": 2}, std_ppr_rules)` returns the expected number

---

## M4 — nflverse crawler

**Goal**: nflverse data lands in `player_stats_raw` and player metadata in `players`.

**Tasks**:
- `crawlers/nflverse/client.py`: wraps `nflreadpy.load_player_stats`, `load_players`, `load_rosters`, `load_schedules`
- Convert from Polars to dicts for storage
- `repository/upsert.py`: idempotent INSERT ... ON CONFLICT helper
- Integration test using small sample parquet from `tests/fixtures/sample_data/`
- Wire into `ff-pipeline run --source nflverse`

**Done when**:
- `ff-pipeline run --source nflverse` runs without error
- `SELECT COUNT(*) FROM player_stats_raw WHERE source = 'nflverse'` returns a sensible number (thousands per season)
- `SELECT COUNT(*) FROM players` returns ~2500 (active NFL roster size × multi-year history)

---

## M5 — NFL.com crawler (current season)

**Goal**: your league's current season data — owners, teams, rosters, matchups, transactions, scoring rules, **and league-wide player availability** — populated.

This is the highest-risk milestone. Allow extra time and expect iteration on parsers.

**Tasks**:
- `crawlers/nfl_com/client.py`: httpx session loaded with cookie; retry logic; auth-failure detection
- `crawlers/nfl_com/urls.py`: URL templates
- `crawlers/nfl_com/parsers.py`: one parser function per page type; use BeautifulSoup with explicit table-class selectors
- `crawlers/nfl_com/league.py`: high-level "scrape this season's data" function
- **`crawlers/nfl_com/availability.py`: scrape the league-wide players page, paginating through all players, producing `player_availability` rows.** This is a NEW responsibility added specifically to support the game-time-state requirements (F1A in spec).
- Save raw HTML to `tests/fixtures/nfl_com_html/` for every page type as you go — these become your regression tests
- **Implement scoring-rule scraper** (`scoring/scraper.py`) — populates `scoring_rules` table from `/settings` page
- **Game-time snapshot logic**: when a sync runs during game day, mark the captured `team_rosters` and `player_availability` rows as `is_pre_kickoff_snapshot=True`. When a sync runs at other times, write the rows but leave `is_pre_kickoff_snapshot=False` (or update the pre-kickoff row if the kickoff hasn't happened yet).
- Integration: parser unit tests against fixtures
- Wire into `ff-pipeline run --source nfl_com`

**Iteration loop**:
1. Implement parser for one page type
2. Run it live against your league
3. Save the response HTML as a fixture
4. Add a unit test
5. Move to the next page type

**Done when**:
- [x] `ff-pipeline run --source nfl_com --season 2025` populates every relevant table for 2025 — verified 2026-05-27: 12 owners, 12 teams, 193 rosters, 12 matchups, 8 transactions, 875 player_availability rows, 1 league, 1 season, 1 pipeline_run, 1 source_health
- [x] `ff-pipeline cookie test` verifies cookie works — exits 0 with "Cookie is valid." on a refreshed cookie
- [~] Auth failure scenario tested — unit tests cover `AuthFailureError` detection paths and the CLI maps it to exit code 77 with an actionable message (`refresh NFL_COOKIE via cookie set`). A full end-to-end test against a tampered cookie was attempted but multi-field cookies make it hard to invalidate just the session field; the path will exercise on its own at the next natural cookie expiry. See `10_OPEN_QUESTIONS.md` §M5-V3.
- [x] All 8 NFL.com page-type parsers have at least one passing fixture-based unit test (real HTML): league_home, owners, team_roster, weekly_matchups, transactions, availability, gamecenter, settings (scoring). 163 tests pass.
- [x] `player_availability` table has at least one row per active *available* player per week scraped with appropriate `status` — all 875 rows tagged `FREE_AGENT` since NFL.com's `/players` URL ships with `playerStatus=available`. Capturing OWNED / ON_WAIVERS rows requires sweep variants, deferred to M9 (`10_OPEN_QUESTIONS.md` §M5-V1).
- [x] Game-time snapshot logic verified — `--snapshot-kind {pre_kickoff,audit}` flag added to `ff-pipeline run`. Live run with `--snapshot-kind pre_kickoff` set `was_locked_at_kickoff=True` on all 193 roster rows and `is_pre_kickoff_snapshot=True` on all 875 availability rows. The default heuristic (`_default_snapshot_kind`) still applies when the flag is omitted.

---

## M6 — Sleeper crawler

**Goal**: projections and trending players from Sleeper.

**Tasks**:
- `crawlers/sleeper/client.py`: httpx with rate limiter (1000/min cap)
- `crawlers/sleeper/endpoints.py`: typed wrappers around the relevant endpoints
- Map Sleeper player IDs to internal `player_id` via the `players.sleeper_id` column (populated by nflverse)
- Score projections through the engine: store `projected_points` already-computed
- Integration test with mocked HTTP responses

**Done when**:
- `ff-pipeline run --source sleeper` populates `projections`
- Trending data appears in a `trending_players` table or as a denormalized JSON column on players

---

## M7 — Normalizer + ID resolution

**Goal**: cross-source joins work. A given player can be queried by any of (name, GSIS ID, Sleeper ID, NFL.com ID).

**Tasks**:
- `normalizer/player_ids.py`: build/update `players` table by merging:
  - nflverse `load_players` (canonical metadata + GSIS, Sleeper IDs)
  - Sleeper `/players/nfl` (Sleeper IDs and NFL IDs as cross-check)
  - NFL.com league pages (NFL.com player IDs scraped from URLs)
- Fuzzy matching for new players where direct ID match fails (`thefuzz` library)
- Manual override table `player_id_overrides` for stubborn cases (e.g., "Marvin Mims Jr." vs "Marvin Mims")
- `normalizer/conflicts.py`: implement precedence rules from `03_DATA_SOURCES.md`

**Done when**:
- Every player on your current roster has a complete row in `players` with all relevant IDs
- Querying by GSIS ID returns the same player as querying by Sleeper ID
- Unit tests cover the 3-4 most likely fuzzy-match failure cases

---

## M8 — FastAPI service

**Goal**: every endpoint in `06_API_CONTRACT.md` returns the documented shape.

**Tasks**:
- `api/main.py`: FastAPI app, structured logging integration
- `api/schemas.py`: Pydantic response models for every endpoint
- `api/routes/`: one file per resource group
- `repository/queries.py`: query functions consumed by routes
- `tests/integration/test_api_endpoints.py`: TestClient-based tests for each endpoint
- Wire `ff-pipeline serve` command
- Open `http://127.0.0.1:8000/docs` and click through every endpoint

**Done when**:
- All endpoints return 200 with the right shape on happy-path
- 404 / 400 handling is consistent
- OpenAPI schema is consumable by future Phase 2 frontend code (try the auto-generated TypeScript client)

---

## M9 — Backfill + scoring verification

**Goal**: every historical season is in the database; scoring engine is verified against NFL.com ground truth.

**Tasks**:
- `scripts/backfill.py`: iterates seasons, calls crawler for each
- Make it **resumable**: track progress in `pipeline_runs` (mode=`backfill`), check what's already done before re-fetching
- Run for your league: `ff-pipeline backfill --start 2014`
- Verification: implement `ff-pipeline verify` command
- Verify 3 named weeks per season; expect 100% match within 0.1 points
- Fix any discrepancies (likely scraping or rule-parsing bugs)

**Done when**:
- Every season from `LEAGUE_START_YEAR` to current is fully populated
- `ff-pipeline verify` passes for at least 3 known good weeks per season
- A bad cookie midway through backfill produces a clean resumable failure (not data corruption)

---

## M10 — Scheduling + observability polish

**Goal**: the system runs itself.

**Tasks**:
- Write the cron config (or systemd timer / launchd plist depending on OS) — see `08_OPERATIONS.md`
- `ff-pipeline status` produces an actionable summary
- Backup cron entry tested
- Log rotation configured (logrotate or built-in)
- Test: simulate a cookie expiration, observe the recovery path

**Done when**:
- Cron is installed and `crontab -l` shows the schedule
- A test backup file appears in `data/backups/`
- `ff-pipeline status` output is readable and useful

---

## M11 — Documentation pass

**Goal**: someone (probably future-you) can pick this up cold and run it.

**Tasks**:
- Project `README.md`: quick-start, link to docs/, common commands
- Update any docs that drifted during implementation
- Write a `RUNBOOK.md` for the common operational scenarios
- Brief `CONTRIBUTING.md` (even though it's just you — convention helps future-you)
- Make sure `pyproject.toml` has accurate description and author fields
- Commit, push to GitHub if desired

**Done when**:
- A fresh clone + `uv sync` + populated `.env` + `ff-pipeline init` + `ff-pipeline run` works end-to-end without consulting other docs

---

## What's deliberately NOT in the roadmap

- **Phase 2 dashboard** — separate effort
- **Cloud deployment** — local cron is sufficient for Phase 1; revisit before/during Phase 2
- **Multi-user / multi-league support** — schema supports it, but no UI/CLI for it yet
- **Real-time scoring** — out of scope
- **Active write-back to NFL.com** — strictly out of scope (Phase 3 may suggest moves; the user executes them in NFL.com)
- **Yahoo / ESPN crawlers** — only relevant if the league moves; can be added as new modules then
