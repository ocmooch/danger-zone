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

### P1-V1. 2010–2015 seasons are unscored (no period rules) — RESOLVED (2026-06-05)

**Resolved.** Rules were *solved from data*, not guessed: every starter-week in
`team_rosters.extra_data` carries NFL.com's actual `nfl_com_points`, so for each
season a labeled `(stat_vector → league_points)` matrix recovers the per-unit
coefficients by least squares, snapped to NFL.com's value vocabulary and
verified back through the real engine. See `scripts/distill_scoring_rules.py`
and `PHASE_PRE2016_PLAN.md`.

Findings:

* **2011–2015 match the current 51-rule set** — confirmed (the solve recovers
  the 2016+ coefficients exactly). Rules loaded (`scoring load --season`),
  rescored, and offline-verified at **89–92% exact-to-cent**, the same long-TD
  ceiling (§P1-V2) as the verified 2016–2025 seasons.
* **2010 is the one distinct era**: **6-point passing TDs + 0.5 PPR (half-PPR)**,
  standardizing to 4-pt / full-PPR from 2011 on. Conclusive (PPR 0 or 1.0 →
  ~19%; pass-TD 4 or 5 → ~82%; the recovered values → 91.5%) and
  **user-confirmed**. Recovered ruleset: `.project-src/dz-rules-2010.csv` (a
  minimal two-line patch of the canonical export). Loaded, rescored, verified.
* **Kicking** is unchanged across all eras (FG-bracket/XP reproduce
  `nfl_com_points` at 100% for 2010–2025).
* **DST**: the real pre-2016 gap was *missing team-defense ingestion*, now
  backfilled (`team-defense --season 2010..2015`) and scored. DST reproduces at
  ~69% (2010–15) vs ~83% (2016–25) — the *same* known DST data-quality tail
  (see the DST re-ingest note), not a rules difference.

**Remaining**: only the long-TD-length bonuses (§P1-V2), which cap *every*
era's verify at the same ~92% ceiling, and the standing DST data-quality
re-ingest. No unobserved-stat flags surfaced.

**Trust-check status (2026-06-07)**: keep the reconstruction marked **not
final** until the offline team-total sanity check is investigated.
`ff-pipeline verify --season 2010 --reconcile` compared 183 team-weeks and
failed 134; `ff-pipeline verify --season 2024 --week 1 --reconcile` failed 9
of 13 rows, including one no-starters artifact. The per-player scored rows
remain useful, but the summed-starters → NFL.com team-total invariant is not
yet reliable enough to close the UP/F27 trust gate.

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

**Resolved — J.J. / Jordy Nelson (rostered subset now fully closed)**: the
held-back stub is folded. The blocker was that NFL.com id `1032` — actually
**Jordy** Nelson's id — had been stamped onto the J.J./Jamarcus canonical row
(17322), dragging Jordy's whole fantasy history (`team_rosters`,
`transactions`) onto J.J. and leaving the real Jordy row (17326) with no
NFL.com side at all. The nflverse stats were never conflated (they split
cleanly by `gsis_id`); the defect was purely the misplaced NFL.com id.
`scripts/untangle_nelson_conflation.py` repoints Jordy's NFL.com rows
17322 → 17326, hands id `1032` back to Jordy, then folds J.J.'s own stub
(id `2552656`) into 17322 — seeding `player_id_overrides` for both so neither
re-stubs. Final spans: Jordy 2010-2018, J.J. 2016-2017. `verify` passes to the
cent for both (J.J. 2017 W1 15.30=15.30, 2016 W15 14.80=14.80; Jordy 2016 W1
15.20=15.20).

**Accepted source gaps (cannot be merged)**: Torry Holt and Vonta Leach have
no nflverse row at all (retired before the 2010 window), so there is nothing
to fold into. Left as honest single-row gaps — *not* pending work; do not
fabricate a canonical identity for them.

**Remaining (irreducible)**: gamecenter-only skips with **no** rostered span
still surface as `our_raw_stats_missing` in `verify` — genuinely ambiguous
abbreviated names (several real players sharing initial+last+position) plus
non-nflverse names. They do not shadow a real player on the players index.
**Owner**: optional. **Fix path**: hand-verify individual cases into
`player_id_overrides` and re-run the affected weeks — no automated guess is safe.

### P1-V4. Misstamped roster identities (the Nelson bug generalized)

**State (RESOLVED)**: the J.J./Jordy Nelson untangle (§P1-V3) turned out to be
one instance of a recurring class — the resolver's fuzzy fallback folded an
abbreviated NFL.com lineup name ("S. Smith") onto a same-name + same-position
player **without checking the lineup's season fell inside that player's NFL
career**, so the wrong row accreted another player's `nfl_com_player_id` +
fantasy history (`team_rosters`, `transactions`) while the real player sat
stranded (stats, but no `nfl_com_player_id`, no roster rows).

A direction-agnostic audit (`scripts/audit_roster_stat_era_mismatch.py` —
rostered before `rookie_year` / disjoint roster∩stat eras / rostered >2y past
`last_season`) found the bug is **bounded, not systemic**: 5 real cases among
1,168 skill players, each with a uniquely-identified stranded owner, all
confirmed by matching the misplaced rows' weekly `nfl_com_points` to the
owner's production:

| misattached onto (kept its own stats) | → true owner |
|---|---|
| Shi Smith (rookie '21) | Steve Smith Sr |
| Jon Brown K (rookie '17) | Josh Brown K |
| Ben Edwards (rookie '15) | Braylon Edwards |
| John Matthews (last '11) | Jordan Matthews |
| Tom Crabtree TE (last '13) | Michael Crabtree WR |

Repaired by `scripts/untangle_misstamped_roster_identities.py` (whole-pile
re-home of the id + NFL.com tables to the owner, override seeded, spans
recomputed). `verify` passes to the cent for the scored-era owners (Michael
Crabtree 2017 W1 14.30=14.30, Jordan Matthews 2016 W1 25.40=25.40); pre-2016
owners are confirmed offline via `nfl_com_points` (no scoring rules <2016).

**Prevention**: `normalizer/player_ids.py` now season-constrains the fuzzy
fallback (`_career_contains`), so reconstruction can't reproduce this — the
right same-era namesake wins. Exact override/direct-ID paths are *not*
constrained (a player legitimately rostered past their career still resolves
by their own id). Re-run the audit after any reconstruction to catch
regressions.

**Accepted (benign, surface in the audit)**: Tim Tebow (rostered to 2021) and
Colin Kaepernick (to 2023) are real players kept on keeper rosters past their
careers — unique names, no younger namesake to confuse, nothing to repair.
Re-run on 2026-06-07 against `data/fantasy.db` found exactly those two
suspects and no new temporal mismatch candidates.

**Known blind spot**: two same-name players whose careers *overlap* (ids
swapped between contemporaries) are invisible to temporal checks. Catching
those needs a name-level cross-check (`nfl_com_player_id` → NFL.com display
name vs canonical name) — deferred to Phase 2.

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

**State**: Reconstruction parses standings and draft pages; remaining history
follow-ups:
- `/league/{id}/history/{year}/draftresults` — draft picks
- `/league/{id}/history/{year}/playoffs` — playoff bracket. Parser and
  reconstruction support now consume the championship-bracket team set to
  classify postseason schedule rows as championship vs consolation; regenerate
  affected seasons before expecting `matchups.is_consolation` in the DB.
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
