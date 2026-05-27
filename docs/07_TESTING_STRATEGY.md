# 07 — Testing Strategy

The pipeline runs autonomously and produces data that downstream phases trust. Testing is therefore not optional — it's how we get confidence that the next time NFL.com tweaks a CSS class, or nflverse adds a column, or the scoring rules need recomputing, we'll catch it in seconds, not weeks.

## Test pyramid

```
                          ┌─────────────────────────────┐
                          │   End-to-end smoke (1-2)    │
                          ├─────────────────────────────┤
                          │   Integration tests (~20)   │
                          ├─────────────────────────────┤
                          │   Unit tests (~80%)         │
                          └─────────────────────────────┘
```

## Unit tests

These are the foundation. Every pure function gets one, especially:

### Scoring engine (the most important)

`tests/unit/test_scoring_engine.py`

- **One test per stat key.** Given a stats dict with only that stat, verify the result.
- **One test per bonus rule.** Verify on/off behavior across the threshold.
- **Composition tests.** Multi-category stat lines verify correct addition + breakdown.
- **Negative stats.** INTs, fumbles produce negative points.
- **Edge values.** Exactly-at-threshold values, zero values, missing keys.
- **Real historical week regression.** A handful of named weeks (e.g., "Lamar Jackson Week 12 2024") with their full nflverse stat lines and the **expected** league point total — pinned in the test as a constant.

Target: **≥95% line coverage on `scoring/engine.py`** measured via `coverage.py`.

### Parsers

`tests/unit/test_parsers.py`

- For each NFL.com page type, save 2-3 sample HTML files to `tests/fixtures/nfl_com_html/`:
  - One "normal" page
  - One "edge case" page (empty roster, mid-season trade, etc.)
  - One "page with auth banner" (logged-out state) — verifies auth-failure detection
- Parser tests load these fixtures from disk, run the parser, assert on the output structure.
- **No network calls** in unit tests.

### Normalizer

`tests/unit/test_normalizer.py`

- ID mapping correctness (given a player by GSIS ID, find their Sleeper ID and vice versa)
- Conflict resolution (multiple sources disagree → primary wins)
- Stat range validation (negative yards rejected, type coercion, etc.)

### Settings & config

`tests/unit/test_settings.py`

- `.env` parsing: required vars missing → clear error
- Cookie redaction in logs
- Path resolution for cache directories

## Integration tests

These exercise multiple modules together but still mock external services where possible.

### `tests/integration/test_pipeline_smoke.py`

The single most important integration test. The "smoke test" runs the full pipeline against:
- A fixture NFL.com HTML response (loaded from disk)
- A canned nflverse dataframe (loaded from a small parquet file in `tests/fixtures/sample_data/`)
- A canned Sleeper response (JSON file)

Then asserts:
- The correct number of rows landed in each table
- A known player's stats appear with the right league-adjusted points
- No errors logged

This test runs in CI on every commit. It must complete in under 30 seconds.

### `tests/integration/test_api_endpoints.py`

For every endpoint in `06_API_CONTRACT.md`:
- Run a request against a test client (`fastapi.testclient.TestClient`)
- Verify response shape matches the documented schema
- Verify status codes for happy-path and 404s

### `tests/integration/test_database_round_trip.py`

For every model:
- Insert a row
- Retrieve it
- Verify equality

For the scoring engine:
- Insert raw stats
- Trigger scoring
- Verify scored row exists with correct totals

### `tests/integration/test_alembic_migrations.py`

- Run all migrations against a fresh in-memory SQLite database
- Then run them down — verifies the migrations are reversible
- Assert final schema matches model definitions

## Data quality tests

These are different from unit/integration tests — they run **against the live data in your database** to catch silent corruption.

`tests/data_quality/` contains pytest functions that run as part of `ff-pipeline run --verify`:

### Required-column completeness

For every table, assert no NULL values in columns marked NOT NULL in the schema.

### Cross-source consistency

For every (player, season, week) with stats from multiple sources:
- Difference in `passing_yards` between nflverse and `api.fantasy.nfl.com` must be ≤ 5 yards (allowing for stat correction lag)
- If difference exceeds threshold, log a warning record to `data_quality_issues` table (not failing the run, but visible in `ff-pipeline status`)

### Scoring round-trip

For 3 known historical weeks, recompute league points using current rules + cached raw stats. The result must equal the previously-stored `total_points` to the cent. If not: scoring engine has regressed.

### Orphan checks

- Every `team_id` in `matchups` exists in `teams`
- Every `player_id` in `team_rosters` exists in `players`
- Every `season_id` in any table exists in `seasons`

## Snapshot tests (HTML fixtures)

The NFL.com scrapers are the most fragile part of the system. Mitigation strategy:

1. **On first successful scrape of a new page type**, save the raw HTML to `tests/fixtures/nfl_com_html/{page_type}/{date}.html`
2. The unit test for that parser loads from this fixture
3. **When NFL.com changes**: the live scrape fails (or returns garbage), but unit tests still pass against the snapshot. The fix workflow is:
   - Update the parser
   - Capture a new HTML snapshot from the live (now-changed) site
   - Save it as `tests/fixtures/nfl_com_html/{page_type}/{date}.html` (kept alongside the old one for now)
   - Update the test to use the new snapshot
   - Verify both snapshots' tests pass — proves backward compatibility for old saved data

This way the system remains capable of re-parsing historical pages even after NFL.com redesigns.

## Test commands

```bash
# Run everything
pytest

# Just unit tests (fast, no I/O)
pytest tests/unit

# Just integration
pytest tests/integration

# Data quality against live database
ff-pipeline run --verify

# Coverage report
pytest --cov=src/ff_pipeline --cov-report=html

# Run a single named test
pytest tests/unit/test_scoring_engine.py::test_passing_yards_bonus_300
```

## What's NOT tested

To stay sane:

- **NFL.com's real responses**. We don't make live HTTP calls to NFL.com in tests. The cookie is user-specific and rotates; tests must be deterministic and offline.
- **nflverse's full dataset**. We mock with small parquet samples.
- **Performance**. There are loose latency targets in the spec (`N4`), but they're operational metrics, not test assertions.
- **Browser rendering**. We don't use a real browser; we test the HTML parser directly.

## Continuous integration

GitHub Actions config (created during implementation):

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install uv
      - run: uv sync
      - run: uv run pytest --cov=src/ff_pipeline --cov-report=term-missing
      - run: uv run ruff check
      - run: uv run mypy src/ff_pipeline
```

Pull requests cannot merge if tests fail. (For a single-user project this is more like a self-discipline gate than a multi-dev safeguard, but worth keeping.)
