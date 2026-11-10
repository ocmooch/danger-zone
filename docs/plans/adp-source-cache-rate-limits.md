# ADP Source Cache + Rate-Limit Hardening

**Repo:** `danger-zone` / `ff-pipeline`  
**Status:** planned, not started  
**Scope:** operational hardening for ADP ingestion only  
**Not a dashboard blocker:** the current live DB already has ADP rows for the dashboard.

## Why This Is Worth Doing

The initial ADP backfill proved the feature path works, but it also exposed an operational weakness:
bulk historical pulls can hit source rate limits. MFL returned HTTP `429 Too Many Requests` during
the live 2023/2024 pulls, even though the same rows had already been fetched successfully into a
scratch DB. The live DB was repaired from that validated scratch copy, but future annual refreshes
should not depend on manual recovery.

ADP is a good fit for caching because historical rows are effectively immutable. Once 2010-2025
source payloads are captured, future reruns should read local raw payloads by default and only touch
external services for new years or explicit refreshes.

## Goal

Make ADP ingestion repeatable and polite:

- Cache raw source responses for historical ADP pulls.
- Avoid refetching immutable historical data unless explicitly requested.
- Respect HTTP `429` / `Retry-After` when a source rate-limits.
- Add source-specific CLI/backfill controls so one source/year can be repaired without rerunning all
  providers.
- Keep `source_health` honest enough to distinguish `no_data`, `rate_limited`, and true failures.

## Non-Goals

- No dashboard changes. The dashboard already consumes raw `player_adp` rows from any source.
- No new ADP source.
- No rework of the broader crawler framework unless the ADP implementation naturally exposes a tiny
  reusable helper.
- No automatic deletion or rewriting of existing ADP rows beyond normal idempotent upsert behavior.

## Current State

ADP ingestion currently has:

- `src/ff_pipeline/crawlers/adp/ffc.py`
- `src/ff_pipeline/crawlers/adp/mfl.py`
- `src/ff_pipeline/crawlers/adp/sleeper.py`
- `src/ff_pipeline/crawlers/adp/runner.py`
- `scripts/backfill_adp.py`

Live DB coverage after the first backfill:

- FFC: 2010-2024
- MFL: 2019-2025
- Sleeper: 2019-2025

Observed issue:

- MFL returned `429` during live 2023/2024 re-pulls.
- A slower immediate retry still returned `429`.
- Validated scratch-copy MFL rows were upserted into live with repair provenance.

## Design

### 1. Raw Response Cache

Add an ADP source-response cache under:

```text
data/source_cache/adp/
  ffc/
  mfl/
  sleeper/
```

Suggested filenames:

```text
data/source_cache/adp/ffc/{year}_{format}_{teams}.json
data/source_cache/adp/mfl/{year}_adp_{format}_{teams}.json
data/source_cache/adp/mfl/{year}_players.json
data/source_cache/adp/sleeper/{year}_projections_regular.json
```

Cache the raw decoded JSON payload, not parsed `AdpEntry` rows. That preserves source provenance and
lets parser fixes be re-run without another network call.

Default behavior:

- Read cache first when present.
- Write successful network responses to cache.
- Use network only when cache is missing or `--refresh` is passed.

### 2. Source Filters

Extend `scripts/backfill_adp.py` with:

```bash
--source ffc|mfl|sleeper|all
--refresh
--cache-dir data/source_cache/adp
```

Optional but useful:

```bash
--requests-per-minute N
```

The most important workflow this enables:

```bash
uv run python scripts/backfill_adp.py --source mfl --start 2026 --end 2026
```

And for repair:

```bash
uv run python scripts/backfill_adp.py --source mfl --start 2024 --end 2024 --refresh
```

### 3. Rate-Limit Handling

Each live source should handle `429` explicitly:

- If `Retry-After` is present, sleep that long and retry.
- If absent, use conservative exponential backoff with a high ceiling.
- Keep retry count finite.
- Surface a typed rate-limit error if exhausted.

Suggested behavior:

- FFC: keep current modest throttle, add `Retry-After` handling.
- MFL: use a much lower default during historical backfills, because it needs two calls per season.
- Sleeper: keep within Sleeper's documented public API rate guidance; add `Retry-After` handling for
  symmetry.

### 4. Source Health Semantics

Today source failures generally become `status="failed"`. Add or standardize a clearer rate-limit
state:

```text
success
no_data
rate_limited
failed
```

For exhausted `429` retries:

- `AdpSourceOutcome.status = "rate_limited"`
- `SourceHealth.status = "rate_limited"`
- `error_message` includes URL/source/year and retry summary.

Do not partially mark a source as `success` unless rows were actually stored.

### 5. Tests

Add focused tests, not broad live-network tests:

- Cache hit avoids HTTP call.
- Cache miss writes raw payload after successful HTTP response.
- `--refresh` bypasses cache.
- MFL `429` with `Retry-After` sleeps/retries using a monkeypatched sleeper.
- Exhausted `429` records `rate_limited`.
- `--source mfl` only runs MFL and leaves FFC/Sleeper untouched.

Fixture tests should use existing HTTP seams (`FfcHttp`, `MflHttp`, `SleeperAdpHttp`) or a small
cache helper with temp directories.

## Implementation Steps

1. Add a tiny cache helper module, likely `src/ff_pipeline/crawlers/adp/cache.py`.
2. Thread cache config into `LiveFfcSource`, `LiveMflSource`, and `LiveSleeperAdpSource`.
3. Add rate-limit retry helpers or source-local handling for `429`.
4. Extend `scripts/backfill_adp.py` with `--source`, `--refresh`, and `--cache-dir`.
5. Update `AdpSourceOutcome.status` docs/types and `runner.py` handling for `rate_limited`.
6. Add tests for cache, source filtering, and 429 behavior.
7. Run focused gate:

```bash
uv run pytest tests/unit/test_sleeper_adp_source.py tests/unit/test_adp_format_map.py tests/unit/test_adp_matcher.py tests/integration/test_adp_crawler.py -q
uv run ruff check src tests
uv run mypy src
```

8. Run one non-live smoke using fixtures/cache.

## Done When

- ADP backfill can be run source-by-source.
- Historical cached payloads are reused by default.
- A `429` no longer looks like a generic parser/network failure.
- MFL can be retried safely for one year without rerunning FFC/Sleeper.
- Tests cover cache hit/miss, refresh, source filtering, and rate-limit status.

## Operational Note For 2026+

When 2026 ADP becomes relevant, the intended workflow should be:

```bash
uv run python scripts/backfill_adp.py --source all --start 2026 --end 2026
```

If one provider rate-limits:

```bash
uv run python scripts/backfill_adp.py --source mfl --start 2026 --end 2026
```

The cache should make subsequent reruns deterministic and avoid repeated source traffic.
