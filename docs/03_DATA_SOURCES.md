# 03 — Data Sources

This is the most consequential document in the package. Picking the wrong data sources for Phase 1 will cause months of pain in Phases 2 and 3. The choices below are based on direct evaluation of every viable source as of late 2025 / early 2026, including availability, licensing, update cadence, stability, and how well each handles the user's specific use case (a private NFL.com league with multi-year history).

## TL;DR — Source selection

| Source | Role | Auth | Stability | License | Why |
|--------|------|------|-----------|---------|-----|
| **NFL.com fantasy HTML** | League-private data (rosters, matchups, transactions, scoring rules) | User session cookie | Medium (DOM can change) | Personal use OK per NFL ToS | Only source for **your** league's data |
| **nflverse** (via `nflreadpy`) | Authoritative NFL player stats & play-by-play | None | High (years of stability) | CC-BY 4.0 / CC-BY-SA 4.0 | Best statistical data, free, nightly-updated |
| **Sleeper API** | Projections, trending players, supplementary player metadata | None | High (public read API) | Free, no published TOS restriction on personal use | Best free projection source; fills gaps |
| **`api.fantasy.nfl.com`** (player-level) | NFL.com's own projections, news, player rankings | None for player endpoints | Medium (undocumented) | NFL ToS | Useful cross-check; lower priority |

## Sources we evaluated and ruled out

| Source | Why ruled out |
|--------|---------------|
| ESPN Fantasy API | User's league is on NFL.com, not ESPN. ESPN API restricted historical data access in Aug 2025 anyway. |
| Yahoo Fantasy API | Same reason — wrong platform. Also requires OAuth app setup. |
| FantasyData / SportsDataIO | Paid, starting at $99/mo. Overkill for personal use. |
| FTN Data direct | $599/year minimum. nflverse already includes a subset of FTN charting data for free under CC-BY-SA. |
| Pro-Football-Reference | nflverse pulls from this and more; redundant. Also more aggressive about scraping pushback. |
| FantasyNerds | Paid; not a fit for personal use scale. |
| RotoWire / RotoBaller | No public API; paid scraping is rude and against ToS. |
| Direct ESPN site scraping | Wrong platform; also more brittle than nflverse. |

---

## Source 1 — NFL.com fantasy HTML (private league)

### Why this and not the `api.fantasy.nfl.com` endpoints

`api.fantasy.nfl.com` has a few public endpoints (player stats, weekly rankings, projections), but **none of them return your private league's data**: rosters, matchups, transaction log, draft results, owner info, etc. For those, you have to authenticate, and the only viable authentication is the browser session cookie — there is no developer key program for fantasy data.

### Authentication

- **Method**: User exports their browser session cookie once. Stored in `.env` as `NFL_COOKIE`.
- **Lifetime**: typically ~30 days with "remember me." Refresh process is documented in `starter/prerequisites.md`.
- **Detection of expiry**: requests redirect to login, or return a page with `id="signin-link"` instead of league content. The crawler must detect this and fail loudly with an actionable error.

### URL structure (verified against open-source scrapers and public NFL.com pages)

Base: `https://fantasy.nfl.com`

For league `{LID}` and season `{YR}`:

| Page | URL pattern | What we parse |
|------|-------------|---------------|
| League home | `/league/{LID}` | League name, owner list, current week |
| League history list | `/league/{LID}/history` | Available historical seasons |
| Season home | `/league/{LID}/history/{YR}` | Final standings for that season |
| Owners | `/league/{LID}/owners` | Owner names, team names, history of who-owned-what |
| Settings | `/league/{LID}/settings` | Scoring rules, roster config, schedule format |
| Draft results | `/league/{LID}/history/{YR}/draftresults` | Round-by-round picks |
| Standings | `/league/{LID}/history/{YR}/standings` | Final regular & playoff standings |
| Weekly matchups | `/league/{LID}/history/{YR}/schedule?scheduleDetail={WK}` | All matchups for week WK |
| Gamecenter (lineups + points) | `/league/{LID}/gamecenter?gameId={GID}` | Both teams' lineups for a specific matchup |
| Transactions | `/league/{LID}/history/{YR}/transactions[?offset={N}]` | Full season diary: trades, waivers, drops, adds, **lineup/start-sit moves, and league/setting changes**. Paginated by NFL.com's shared `?offset=` widget — swept page-by-page (only page 1 used to be fetched). |
| Managers (per season) | `/league/{LID}/history/{YR}/owners` | Per-season team logos (the avatar backfill snapshots these onto `teams`). |
| Team home | `/league/{LID}/team/{TID}` | Roster, schedule, transaction log |
| **League-wide players (current)** | `/league/{LID}/players?statCategory=stats&statSeason={YR}&statType=weekStats&statWeek={WK}` | **Every player in the league universe** with status (owned by team / free agent / on waivers). Source of truth for `player_availability` table. |
| **Waiver claim queue** | `/league/{LID}/waivers` | Active claims and their priority/clear-time (current season only — not historical) |

### Parsing approach

Every page renders structured HTML tables. The strategy is:

1. **Fetch** with `httpx` (one request per page, with the user cookie).
2. **Parse** with `BeautifulSoup` (lxml backend) against **known table classes** (`tableType-team`, `tableType-rosterTrades`, etc.).
3. **Validate** the table headers match expected columns — if not, log a parse error and skip.
4. **Save raw HTML** alongside the parsed result in the first run of each new season, so we have a fixture for regression testing.

### Rate limiting

- 1 request per 2 seconds (well below anything that would trip NFL.com defenses)
- Backfill of a 10-season league with ~17 weeks each ≈ 1700 pages ≈ 1 hour with the 2s delay
- Concurrent requests are NOT used here (single-threaded, polite)

### Robustness against DOM changes

NFL.com's fantasy site is **not** rapidly evolving — its UI has been substantively stable for years. But when changes do happen, the response is:

1. Parse failure logs the exact selector that failed
2. The raw HTML for the failing page is saved to `data/parse_failures/{timestamp}/`
3. The user (or Claude Code) inspects the saved HTML, updates the parser, the test fixture, and re-runs

The package keeps every parser's selectors **in one file** (`crawlers/nfl_com/parsers.py`) — no scattering — to minimize the editing footprint.

### What we DON'T scrape from NFL.com

- Player-level career stats (use nflverse — same data, cleaner, no scraping)
- Player projections (use Sleeper — same data, free API)
- News articles, expert advice (out of scope for Phase 1)
- Anything from `fantasy.nfl.com` that requires a non-owner authentication context

---

## Source 2 — nflverse via `nflreadpy`

### Why this is the gold standard

`nflverse` is a maintained, automated data infrastructure that publishes NFL data to GitHub Releases nightly. Originally an R ecosystem (`nflreadr`, `nflfastR`), the Python port is `nflreadpy`. The data is the same data professional analysts use; it's not user-generated. Coverage runs from **1999 to present** for play-by-play, with weekly player stats also reaching back to 1999.

### Critical recent change

`nfl_data_py` was **archived September 25, 2025**. Do not use it. Use `nflreadpy` instead. `nflreadpy` is API-compatible enough to drop in but uses Polars instead of pandas. Convert to pandas with `.to_pandas()` if needed.

### What we pull

| nflreadpy function | What it returns | Frequency |
|---|---|---|
| `load_player_stats(years)` | Weekly stats per player (passing/rushing/receiving/kicking/defense) | Every run |
| `load_pbp(years)` | Play-by-play (for opportunity stats: targets, redzone touches, snap %) | Weekly |
| `load_rosters(years)` | NFL team rosters, position eligibility | Weekly |
| `load_schedules(years)` | NFL game schedule, results, bye weeks | Weekly |
| `load_injuries(years)` | Injury reports | Weekly during season |
| `load_players()` | Player metadata + cross-platform IDs (`gsis_id`, `sleeper_id`, `espn_id`, etc.) | Weekly |
| `load_ff_playerids()` | Fantasy-platform-specific ID map | Once per season |
| `load_snap_counts(years)` | Snap counts per game | Weekly |

> **NB**: `load_injuries` may not return 2025+ data — the upstream source died after 2024. Treat injury data as best-effort; primary injury status will come from Sleeper.

### Update schedule (per nflverse documentation)

- Play-by-play data within ~15 minutes of game end
- Re-updated Tuesday → Wednesday night for NFL stat corrections (this is when scoring gets finalized)
- **Recommendation**: schedule Phase 1 in-season runs for **Wednesday late night** to catch the cleanest, fully-corrected data

### Caching

`nflreadpy` caches downloads locally. The pipeline uses a cache directory at `data/nflverse_cache/` and clears it on `ff-pipeline run --no-cache`.

### Licensing

- Most data: **CC-BY 4.0** (attribution required — we satisfy this in the API response footer + README)
- FTN charting data subset: **CC-BY-SA 4.0** (attribution AND derivative works share-alike — relevant if we ever publish; not for personal use)

---

## Source 3 — Sleeper API

### Why even though the league isn't on Sleeper

Sleeper offers the best **free** projection API. Their data is competitive with paid sources for fantasy purposes (they're a top-tier consumer fantasy platform), and their public read endpoints require no authentication.

### Endpoints we use

| Endpoint | Purpose |
|---|---|
| `GET https://api.sleeper.app/v1/players/nfl` | Full player roster with `sleeper_id` → for ID mapping. Called once per day max (response is ~5MB). |
| `GET https://api.sleeper.com/projections/nfl/{year}/{week}?season_type=regular` | Weekly projections by player |
| `GET https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=25` | Trending adds (signal for waiver priority research) |
| `GET https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=24&limit=25` | Trending drops |
| `GET https://api.sleeper.com/stats/nfl/{year}/{week}?season_type=regular` | Cross-check stats against nflverse |

### Rate limiting

- Sleeper publishes: stay under **1000 requests/minute** to avoid IP block
- Phase 1 will never come close — maybe 5-10 calls per pipeline run

### Limitations

- No private league data for us — we don't have a Sleeper league
- Projections are season-wide, not deeply personalized
- API is well-documented but informally — no SLA

---

## Source 4 — `api.fantasy.nfl.com` (lower priority)

### Public, no-auth endpoints that work

| Endpoint | Notes |
|---|---|
| `/v2/players/weekstats?season={Y}&week={W}` | Per-player weekly stats from NFL.com itself |
| `/v2/players/weekprojectedstats?season={Y}&week={W}` | NFL.com's own projections |
| `/v2/players/researchinfo?statType=seasonStats&season={Y}` | Season-level rankings |

These are useful as a **cross-check** against nflverse and Sleeper. We treat them as a tertiary source: pull them, store them, but if they conflict with nflverse, nflverse wins.

### Why not primary

The endpoints are **undocumented** — they could change without notice, and they have changed historically (the `/v1/` versions returned by older blog posts are gone). nflverse is more reliable.

---

## How conflicts are resolved (the normalizer's job)

When multiple sources have data on the same (player, week), we have explicit precedence:

| Data | Primary | Secondary | Tertiary |
|------|---------|-----------|----------|
| Player identity (name, position, team) | nflverse `load_players` | NFL.com league pages | Sleeper |
| Weekly stats (raw) | nflverse `load_player_stats` | NFL.com `api.fantasy.nfl.com` | Sleeper |
| Weekly projections | Sleeper | `api.fantasy.nfl.com` weekprojectedstats | (none) |
| Injury status | Sleeper | nflverse `load_injuries` (if available) | (none) |
| **League roster, matchup, transaction** | NFL.com league HTML (only source) | (none) | (none) |
| **Scoring rules** | NFL.com `/settings` page | (none) | (none) |

Sources are **always** all stored (in `_raw` tables, source-tagged). The "primary" choice only determines what feeds into the normalized, joined views. This means a future code change can switch primaries without re-running ingestion.

---

## Ongoing source-monitoring

The pipeline writes a `source_health` row per run per source: last successful response, response status, row count delta, parse failure count. The `ff-pipeline status` command summarizes the latest. This is the early-warning system for "NFL.com changed something" or "nflverse hasn't published this week yet."
