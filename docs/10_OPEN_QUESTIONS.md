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

## Questions to revisit at end of Phase 1

These should be re-evaluated before starting Phase 2:

- Has any data source become unreliable? Replacement needed?
- Is SQLite still adequate, or are query patterns demanding PostgreSQL?
- Is the API contract complete enough for Phase 2 needs, or are endpoints missing?
- Has Phase 1 surfaced any data quality issues that should be addressed in Phase 2's design?
- Did fine-grained availability tracking (Q7) become necessary? Did off-season cadence (Q8) miss anything important?
