# 10 — Open Questions & Follow-up Work

Live tracker of what is still **open** after the v1.0.0 Phase 1 release
(2026-05-29). Confirmed decisions, settled architecture defaults, and the
deferred questions resolved during Phase 1 (Q1–Q4, Q6–Q8) have moved to
`10_OPEN_QUESTIONS_ARCHIVE.md`.

Owners that previously read `M9` / `M10` have been re-pointed to **Phase 2**:
those Phase 1 milestones shipped via the reconstruction path, and the items
below were explicitly carried past them.

---

## Phase 1 data gaps (known, bounded, documented)

These are real limits of the v1.0.0 dataset — not bugs. Each has a clear fix
path once its input becomes available.

### P1-V1. 2010–2015 seasons are unscored (no period rules)

**State**: The current ruleset (`.project-src/dz-rules.csv`, 51 rules) is
loaded for **2016–2025** and `verify --sweep`-confirmed against NFL.com. We
have no evidence the same rules held for **2010–2015**, and NFL.com history
pages expose final points but not the era's rule table. Reconstruction
populated standings/matchups/lineups for all of 2010–2025, but
`player_stats_scored` is intentionally empty for 2010–2015. nflverse raw
stats for those years remain available, so they can be scored retroactively.

**Fix path**: source period-correct rules → `scoring load --season <YR>` →
`rescore --season <YR>` → `verify --sweep --season <YR>`. Rationale in
`PHASE1_COMPLETION_PLAN.md` §0.2, §5.

### P1-V2. Long-TD-length bonuses are unscored (data-source gap)

**State**: Residual non-DST `verify` deltas are a steady ~12–18/season of
clean **−1 / −4 / −8** under-scores — the `*_yards_bonus_long_td_40/_50`
bonuses (40+ yd TD = +1, 50+ yd stacks to +4). The rules exist in
`scoring_rules`, but nflverse weekly aggregates carry no per-TD distance, so
the engine never gets a count. Documented out-of-scope in
`crawlers/nflverse/stat_keys.py` and `05_SCORING_ENGINE.md`. Deltas come in
pairs (the same long TD credits passer and receiver).

**Owner**: Phase 2. **Fix path**: ingest nflverse play-by-play (`load_pbp`),
derive per-week 40+/50+ yd TD counts into `player_stats_raw`, map to the
existing `*_bonus_long_td_*` keys, `rescore`. Engine + rules already handle
the keys; only the input values are missing.

### P1-V3. Ambiguous abbreviated names can't be auto-merged

**State**: Gamecenter lineups render abbreviated names ("E. Pineiro").
`scripts/merge_split_player_identities.py` folds stubs onto the nflverse row
by (first-initial, last-token, position) — 539 merges applied — but
**deliberately skips** when two real players share that key, because a wrong
fold would mis-attribute another player's stats.

The **league-rostered** subset of these skips (stubs that hold a
`first/last_rostered_season` span, so they shadow a real player on the
players index) is now resolved: `scripts/merge_roster_name_stubs.py` carries
a hand-verified `nfl_com_player_id` → canonical map, applies it through the
same FK-repoint / stamp path, and **seeds `player_id_overrides`** so future
roster syncs resolve directly and never re-stub (48 merges). Two root causes
were behind these specific skips: (a) a `_normalize` bug that ate a leading
`V.`/`I.`/`X.` initial as a roman-numeral suffix, hiding *unique* matches
(Victor Cruz, Vernon Davis …) — now fixed with a regression test; and (b)
nflverse storing **legal** first names (Torrey Smith = `James Smith`, Duke
Johnson = `Randy Johnson`), which defeats first-initial keys — handled by the
curated map (David **and** Duke Johnson both resolved).

**Remaining**: gamecenter-only skips with no rostered span still surface as
`our_raw_stats_missing` in `verify`. One rostered stub (J.J. Nelson, ARI
2016-2017) is **held back** because its obvious canonical row (17322) is
itself a pre-existing J.J./Jordy Nelson conflation — that row carries a
2010-2018 span though J.J. debuted in 2015. Untangle 17322 first, then fold.
Two more (Torry Holt, Vonta Leach) have no nflverse row at all (retired
before the 2010 window) and are left as honest single-row gaps.

**Owner**: optional manual cleanup. **Fix path**: extend the curated map in
`merge_roster_name_stubs.py` (or `player_id_overrides`) for the gamecenter
remainder, then re-run reconstruction for the affected weeks.

---

## NFL.com crawler follow-ups (deferred past Phase 1)

Surfaced during the M5 live run; carried past the Phase 1 milestones they
were originally tagged to.

### M5-V1. Availability sweep only sees `FREE_AGENT` rows

**State**: All rows persisted to `player_availability` are tagged
`status='FREE_AGENT'` — NFL.com's players URL bakes `playerStatus=available`
into its pagination, so OWNED / ON_WAIVERS rows never appear there. OWNED is
implicit from `team_rosters`; ON_WAIVERS is captured nowhere yet.

**Owner**: Phase 2. Deferred deliberately — the sweep variants only pay off
once waiver analytics consume them, and in the offseason every player is
unowned (a live sweep would capture nothing). Historical ownership is already
covered by gamecenter-backed `team_rosters` (2010–2025). **Fix path**: sweep
`playerStatus=owned` and `playerStatus=waivers` URL variants.

### M5-V2. Transactions page is paginated; runner reads only page 1

**State**: The transactions log surfaced only 8 records (all week 17) for
2025 because the runner doesn't iterate NFL.com's `?offset=` pagination.
Pre-week-17 transactions are missing. (Confirmed still unimplemented as of
v1.0.0 — `history.py` reconstructs standings/matchups/lineups only.)

**Owner**: Phase 2. **Fix path**: sweep `?offset=` on
`/history/{year}/transactions`, mirroring `sweep_availability`.

### M5-V3. Live auth-failure path not verified end-to-end

**State**: Unit tests cover `AuthFailureError` detection and the CLI exit-77 +
"refresh `NFL_COOKIE`" message, but the full stale-session → exit-77 → friendly
message chain is verified only by inspection + units. A manual test by flipping
one cookie char didn't invalidate the (multi-field) session.

**Owner**: next natural cookie expiry — the cookie will go stale on its own and
the next scheduled run will exercise the path. Add a `source_health` row check
then. Related: M5-V7.

### M5-V4. Off-by-one in player-page pagination

**State**: The "Next" link advances `offset` by 26 while rendering 25 rows.
The manual-advance fallback compensates, and the live end-of-season sweep hit
875/875 rows, so no data was lost there — but it's unconfirmed on a denser
mid-season week.

**Owner**: Phase 2. **Fix path**: advance by `current_offset + len(rows)`
instead of trusting the "Next" href; verify against a mid-season capture.

### M5-V5. Pages not yet parsed — capture fixtures before building

**State**: Reconstruction parses standings; still **unparsed** and needed for
fuller history:
- `/league/{id}/history/{year}/draftresults` — draft picks
- `/league/{id}/history/{year}/playoffs` — playoff bracket
- `/league/{id}/gamecenter?gameId={N}` — game-ID-keyed gamecenter
  (distinct from the `teamgamecenter` URL we do parse)
- `playerStatus=owned` / `playerStatus=waivers` players-page variants (M5-V1)

**Owner**: Phase 2. Capture real HTML for each first, then iterate selectors
with the "real fixture → test → runner" loop that worked in M5.

### M5-V6. Trade-row markup never exercised against a real fixture

**State**: `parse_transactions` has a "Trade" branch (two records sharing an
NFL.com txn id), but the 2025 fixture had no trades, so it's only
structurally reasoned about — no `transactions_with_trade.html` fixture exists.

**Owner**: Phase 2. Capture a historical transactions page with ≥1 trade, save
as `tests/fixtures/nfl_com_html/transactions_with_trade.html`, add a test.

### M5-V7. Cookie refresh cadence still uncharacterized

**State**: The cookie's natural TTL is unknown. The scheduled weekly sync will
silently start failing on expiry; the pipeline writes a `source_health`
`auth_failure` row, but the operator must notice.

**Owner**: Phase 2 ops. **Fix path**: run `cookie test` ahead of each
scheduled sync and emit a notification on failure; record observed TTLs in
project memory to tune the schedule. Related: M5-V3.

---

## Player-identity follow-ups (deferred past Phase 1)

### M7-V1. No end-to-end "every roster player has all IDs" audit

**State**: `PlayerResolver` has full unit coverage, but no live run asserts
that every rostered player has `{gsis_id, sleeper_id, nfl_com_player_id}`
populated. No audit script exists yet.

**Owner**: Phase 2. **Fix path**: add `scripts/audit_player_ids.py` to walk the
roster and assert resolution per row; holes become `player_id_overrides`
candidates.

### M7-V2. No CLI to resolve cross-source ID conflicts

**State**: On conflicting IDs across sources the resolver logs a warning and
refuses to overwrite the incumbent; the operator must edit
`player_id_overrides` by hand — there's no CLI command (confirmed absent in
`cli.py` as of v1.0.0).

**Owner**: Phase 2 ops. **Fix path**: add e.g. `ff-pipeline normalize override
add --kind sleeper_id --value 9999 --player-id 42`.

---

## Parked for Phase 3

### Q5. Projections vs actual variance tracking
Phase 1 stores raw projections + actuals; computing variance is a Phase 3 /
dashboard concern. No precomputation in Phase 1.

---

## Phase 2 entry review (was "revisit at end of Phase 1" — now due)

v1.0.0 closes Phase 1, so re-evaluate these before Phase 2 design:

- Has any data source become unreliable? Replacement needed?
- Is SQLite still adequate, or are query patterns demanding PostgreSQL?
- Is the API contract complete enough for Phase 2, or are endpoints missing?
- Did Phase 1 surface data-quality issues to address in Phase 2 design?
  (P1-V1/V2/V3 are the known ones.)
- Did the default availability granularity (archived Q7 / M5-V1) or off-season
  cadence (archived Q8) miss anything important in practice?

---

## Locked in — do not re-litigate

Stated so they aren't reopened in later sessions:

- **No IDP.** The league rosters `QB/RB/WR/TE/K` + team DEF only. nflverse's
  full player universe is filtered to `RELEVANT_POSITIONS` at ingestion (both
  the `load_players` metadata pass and the stat-stub path) and legacy rows
  were removed via `prune-players` in two passes: fully-orphaned rows
  (25,355 → 8,587 on 2026-06-01) and then referenced-but-irrelevant
  IDP/OL players (8,587 → 3,093 on 2026-06-01, cascading ~305k incidental
  stat/projection rows). Do **not** re-widen position scope or re-ingest
  pre-`LEAGUE_START_YEAR` retirees on the assumption IDP might be wanted
  later — it won't be. The prune **protects** any player referenced by
  `team_rosters` / `transactions` / `player_availability` /
  `player_id_overrides` regardless of position label, because those rows are
  ground truth and position strings are not (NFL.com scrape artifacts on team
  defenses, rostered fullbacks, two-way players). So no roster/transaction
  data was discarded — only incidental nflverse/Sleeper rows for players this
  league can never field.
- **Python**, not Go/TypeScript — the NFL/fantasy data ecosystem is Python.
- **nflreadpy**, not `nfl_data_py` (archived as of 2025).
- **Static HTTP + BeautifulSoup**, not Playwright — NFL.com renders server-side.
- **SQLAlchemy 2.0**, not raw SQL or Django ORM.
- **alembic** migrations, not drop-and-recreate.
- **FastAPI**, not Flask — auto-OpenAPI.
- **structlog**, not stdlib logging or loguru.
- **uv** for dependencies, not pip/poetry/pdm.
- **typer** for CLI, not argparse/click.
- **pytest**, not unittest.
- **ruff** for lint + format (replaces black + flake8 + isort).
- **mypy** type checking — Phase 1 is fully typed.
- **pre-commit** hooks for ruff + mypy on every commit.
