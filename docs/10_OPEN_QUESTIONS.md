# 10 — Open Questions & Default Decisions

The user has explicitly confirmed three core decisions; remaining defaults are clearly marked. Each remaining default is reversible without major rework.

## Decisions confirmed by the user

### 1. League is currently active on NFL.com ✓ CONFIRMED

**Implication**: The crawler targets both the current season AND historical seasons. Scheduled in-season runs are enabled. Cookie refresh workflow is critical (current league data depends on it).

### 2. 10+ years of NFL.com history ✓ CONFIRMED

**Implication**: Backfill is the long pole — ~1 hour with rate limiting. The pipeline is resumable. We need to verify that NFL.com still serves historical pages going back that far (their `/history/{YR}` pages typically remain accessible to former participants, but very old leagues sometimes have data purged).

### 3. Scoring + game-time state requirements ✓ CONFIRMED

The user explicitly requested:
- Standard scoring + PPR variants ✓
- Custom bonuses (long TDs, yardage tiers) ✓
- **Player waiver status at game time** (Owned / Free Agent / On Waivers) ✓
- **Player roster spot at game time** (Starter / Bench / IR) ✓
- **Date added to / dropped from team** ✓
- **Schema must be extensible to new sections** ✓

These flow into:
- `scoring_rules` table with full bonus support (see `05_SCORING_ENGINE.md`)
- New `player_availability` table for league-wide player state (see `04_DATA_MODEL.md`)
- Extended `team_rosters` table with `acquisition_date`, `drop_date`, `was_locked_at_kickoff` (see `04_DATA_MODEL.md`)
- `extra_data` JSON column on most entity tables for opportunistic capture
- M5 in roadmap explicitly scopes scraping the league-wide players page

## Defaults applied (not explicitly answered)

The following were defaulted because the input tool didn't capture selections. Tell me which (if any) need changing:

### 4. Local-first, cloud-ready architecture (DEFAULT)

**Implication**: SQLite, cron, `.env` files. Everything works on a laptop. Cloud migration path is clearly documented (SQLAlchemy + alembic make it a `DATABASE_URL` change).

**To change to cloud-from-day-1**: tell me; primary changes are: PostgreSQL instead of SQLite, secrets in a vault, scheduler becomes something like Fly.io scheduled tasks or systemd timers in a small VM.

### 5. Python 3.11+ (DEFAULT)

**Implication**: Modern type syntax, the entire NFL data ecosystem (nflreadpy, sleeper-api-wrapper, etc.) is Python-native.

**To change to TypeScript**: tell me; the design is mostly portable but the data sources are weaker in TS (you'd have to wrap nflverse data via CSV downloads or call into Python). Strongly recommend Python here.

### 6. SQLite default with PostgreSQL migration path (DEFAULT)

**Implication**: One file at `data/fantasy.db`. Easy backup. Single-writer at a time.

**To change to PostgreSQL from day 1**: tell me; same schema works, but you'll need a running Postgres instance (Docker, local install, or hosted).

---

## Questions deferred to implementation

These don't block the design but will need answers as you build:

### Q1. How do you want to handle "deleted from NFL.com" data?

**Context**: NFL.com sometimes purges old league data (typically 7+ year old leagues from inactive accounts). If your league was inactive any year, those records may be unrecoverable.

**Default**: Scrape what's available; log a warning when historical seasons return empty pages; don't fail the run.

**Decision point**: Do you want to write a one-time "memory dump" of what's there NOW so we don't lose it later? (Suggested: yes, run backfill ASAP.)

### Q2. What's the ID for unknown players in your league's history?

**Context**: Players who appeared on a roster but whom nflverse doesn't recognize (deep waiver-wire kickers, practice squad call-ups, DEF placeholders). They'll have NFL.com IDs but no GSIS / Sleeper IDs.

**Default**: Insert them with NULL for missing IDs. They get 0 league points unless raw stats also come from NFL.com.

**Future enhancement**: Add a manual `unknown_players_resolution` step to the M7 milestone.

### Q3. How aggressive should the verifier be?

**Context**: The scoring verifier compares our computed points to NFL.com's stored points. There WILL be tiny differences (rounding, stat corrections that landed after our fetch).

**Default**: Tolerance is 0.1 points; differences logged but don't fail the run.

**Decision point**: If you find that's too tight or too loose, adjust `SCORING_VERIFY_TOLERANCE` in `.env`.

### Q4. Should we save raw HTML responses long-term?

**Context**: For audit/forensics, every successful NFL.com page fetch could be saved to `data/raw_pages/{date}/{url_path}.html`.

**Default**: NO, by default. Storage grows quickly. Enable via `SAVE_RAW_HTML=true` in `.env` if you want it. The test fixture HTMLs serve as smaller-scale audit.

### Q5. What about projections vs actual variance tracking?

**Context**: Useful for Phase 3. Phase 1 stores the raw data; computing variance is a Phase 3 task.

**Default**: No precomputation. Variance is a Phase 3 / dashboard concern.

### Q6. Cookie storage — `.env` vs OS keychain?

**Context**: `.env` is fine for the cookie if you trust your file system. OS keychain (macOS Keychain, etc.) is more secure but adds complexity.

**Default**: `.env`.

**To upgrade**: Implement `keyring`-based fetching in `settings.py`; the rest of the code consumes it identically.

### Q7. How granular do you want availability tracking?

**Context**: We've decided to capture one canonical "pre-kickoff" snapshot per (player, week). But NFL.com supports finer granularity — e.g., a player added Tuesday and dropped Thursday would be invisible at the weekly snapshot level.

**Default**: One pre-kickoff row per (player, week); the transaction log captures intra-week moves; we DON'T create additional `player_availability` rows for mid-week states unless we observe them in the wild.

**To change to fine-grained**: enable per-day polling and `is_pre_kickoff_snapshot=False` rows for each intermediate observation. Storage grows ~7×.

### Q8. Off-season sync cadence?

**Context**: After Super Bowl through the next draft (~Feb–Aug), there's still league activity (trades, keepers, retirements, draft).

**Default**: Weekly sync on Sundays. Adequate for catching keeper deadlines, free agency moves, retirements.

**To change**: adjust the cron schedule in `08_OPERATIONS.md`.

---

## Things that ARE NOT open questions (locked in)

These are stated here so they're not re-litigated later:

- **Python**, not Go or TypeScript. The NFL/fantasy data ecosystem is overwhelmingly Python.
- **nflreadpy**, not `nfl_data_py`. The latter is archived as of 2025.
- **Static HTTP + BeautifulSoup**, not Playwright. NFL.com renders server-side; Playwright would be overkill.
- **SQLAlchemy 2.0**, not raw SQL or Django ORM. Type-safe, future-proof, lets the DB swap easily.
- **alembic for migrations**, not "drop and recreate". You will have data you don't want to lose.
- **FastAPI**, not Flask. Auto-OpenAPI generation is too valuable to give up.
- **structlog**, not stdlib logging or loguru. Structured JSON logs that work with future log aggregators.
- **uv for dependency management**, not pip/poetry/pdm. Fast, modern, well-maintained by astral.sh.
- **typer for CLI**, not argparse or click. Same author as FastAPI; consistent type-driven syntax.
- **pytest for testing**, not unittest. Industry default.
- **ruff for linting + formatting**, not black + flake8 + isort. Ruff replaces all three.
- **mypy for type checking**. Phase 1 is fully typed.
- **pre-commit hooks** for ruff and mypy on every commit.

---

## Future manual-access work (surfaced during M5 verification)

These were discovered while running the live NFL.com crawler against the
real league. Each is a concrete follow-up but explicitly **out of M5
scope**. They will get picked up by the milestone listed beside each.

### M5-V1. Availability sweep only sees `FREE_AGENT` rows

**Observed (2026-05-27 live run)**: All 875 rows persisted to
`player_availability` for `(season_year=2025, week=17)` were tagged
`status='FREE_AGENT'`. NFL.com's `/league/{id}/players?...` URL ships
with `playerStatus=available` baked into the rendered pagination links,
so OWNED and ON_WAIVERS rows never appear via that endpoint.

**Implication**: The `player_availability` table currently captures
only the *available* slice of the player universe. Owned players'
status is implicit from the `team_rosters` table, but ON_WAIVERS
players are not captured anywhere yet.

**Owner**: M9 (backfill) or whichever milestone first needs full
status coverage. Likely fix: have the runner sweep `playerStatus=owned`
and `playerStatus=waivers` URL variants in addition to the default.

### M5-V2. Transactions page is paginated; runner only reads page 1

**Observed**: The transactions log on the real league surfaced only 8
records (4 Add + 4 Drop) for the 2025 season, all from week 17. The
parser handled them correctly. NFL.com's history transactions page is
paginated by `?offset=` like the players page, but the runner doesn't
iterate.

**Implication**: Pre-week-17 transactions are not in the DB.

**Owner**: M9 backfill (historical seasons need this anyway) — wire
a sweep over the `?offset=` pagination on `/history/{year}/transactions`,
mirroring `sweep_availability`.

### M5-V3. Live auth-failure path not end-to-end verified

**Observed**: Unit tests cover `AuthFailureError` detection (signin
marker, 302 to id.nfl.com, `test_auth()` returning False) and the CLI
maps `AuthFailureError` → exit code 77 with the actionable
"refresh NFL_COOKIE via `ff-pipeline cookie set`" message. A manual
end-to-end test was attempted by flipping a single character mid-cookie
in `.env`, but the run still authenticated successfully — NFL.com
cookies are multi-field, and flipping one char usually lands in a
non-session field (timestamp, locale, GDPR consent token).

**Implication**: The full chain "stale session → AuthFailureError →
typer.Exit(77) → friendly message" is verified only by code inspection
+ unit tests, not by a real end-to-end run.

**Owner**: Reviewed at the natural next cookie expiry — the cookie
will become invalid on its own and the next scheduled run will exercise
the path properly. Add a `source_health` row check at that point.

### M5-V4. Off-by-one in NFL.com player-page pagination

**Observed**: The "Next" link on each `/league/.../players?...` page
advances `offset` by 26, not 25, even though the page renders 25 rows
("1 - 25 of 875"). The sweep's manual-advance fallback handles this
correctly, but it means at least one row may be skipped at each page
boundary.

**Implication**: Live sweep reported 875 unique rows for a stated
total of 875, so in practice the off-by-one doesn't lose data here —
NFL.com appears to be 1-indexed internally and our `offset=PAGE_SIZE*N`
fallback hits the right slice. Worth confirming on a non-end-of-season
week, when the player universe is denser.

**Owner**: M9 — switch to a `current_offset + len(rows)`-based advance
instead of trusting NFL.com's "Next" href, and verify against a
mid-season capture.

### M5-V5. Some pages NOT in M5 — captured fixtures available

The user-captured `teamgamecenter.html` lives at
`/history/2025/teamgamecenter?teamId=N` (the per-team weekly view).
This is the gamecenter parser's fixture. **Other pages that M9
backfill will need but M5 doesn't parse**:

- `/league/{id}/history/{year}/draftresults` — draft picks
- `/league/{id}/history/{year}/standings` — final standings
- `/league/{id}/history/{year}/playoffs` — playoff bracket
- `/league/{id}/gamecenter?gameId={N}` — game-ID-keyed gamecenter
  (distinct URL pattern from teamgamecenter)
- A `playerStatus=owned` and `playerStatus=waivers` variant of the
  players page (see M5-V1)

**Owner**: M9. Have the user capture HTML for each before that work
starts; iterate selectors using the M5 "real fixture, then test, then
runner" loop that worked here.

### M5-V6. Trade-row markup not exercised against a real fixture

**Observed**: `parse_transactions` includes a "Trade" branch that emits
two `ParsedTransaction` records sharing an NFL.com transaction id, but
the real 2025 fixture contained no trade rows (only Add/Drop/Lineup).
The branch is unit-tested only indirectly (by structural reasoning).

**Owner**: Manual capture of a historical season's transactions page
that includes at least one trade (M9 backfill will hit one). Save as
`tests/fixtures/nfl_com_html/transactions_with_trade.html` and add a
parser test.

### M5-V7. Cookie refresh cadence still uncharacterized

**Observed**: The cookie the user pasted on 2026-05-27 was refreshed
just before this session. We do not know the natural TTL.

**Implication**: M10's cron schedule (weekly Sunday sync) will silently
start failing once the cookie expires. The pipeline does write a
`source_health` row with `status='auth_failure'` when that happens, but
the operator has to notice.

**Owner**: M10 ops — add a "cookie staleness" check that runs `cookie test`
ahead of every scheduled sync and emits a desktop notification (or
similar) on failure. Track observed TTLs in the project memory so the
schedule can be tuned.

## Questions to revisit at end of Phase 1

These should be re-evaluated before starting Phase 2:

- Has any data source become unreliable? Replacement needed?
- Is SQLite still adequate, or are query patterns demanding PostgreSQL?
- Is the API contract complete enough for Phase 2 needs, or are endpoints missing?
- Has Phase 1 surfaced any data quality issues that should be addressed in Phase 2's design?
- Did fine-grained availability tracking (Q7) become necessary? Did off-season cadence (Q8) miss anything important?
