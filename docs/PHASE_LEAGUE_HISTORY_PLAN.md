# League-History Media & Event-Log Plan

**Status:** code complete (tests green) · backfill not yet run on live data · **Owner:** ocmooch · **Drafted:** 2026-06-05 · **Branch:** `feature/league-history-media-and-event-log` (cut from `dev`)

> **Implementation note (2026-06-05).** Both workstreams are built, tested,
> and migrated (single Alembic head `b2c3d4e5f6a7`). What remains is running
> the scrapers against live NFL.com (needs the cookie in `.env`):
> - **A:** the per-season scrape now uses `sweep_transactions`, so a normal
>   `run_nfl_com` per season backfills the full diary. Re-run all seasons,
>   then check the §5 counts (adds/drops/trades realistic; earliest non-draft
>   `executed_at` back in Sept/Oct; `lineup_change` + `setting_change` rows
>   with `extra_data`).
> - **B:** run `media.backfill_team_avatars(session, client, league_id=…)`
>   (reads the per-season `history/{yr}/owners` page) to populate `assets` +
>   `teams.team_avatar_asset_id`. Not wired into a CLI command yet.
>
> Setting/commish (`setting_change`) parsing is best-effort: no fixture row
> was available, so the row→payload mapping is keyword-driven and stores the
> raw From/To/description in `extra_data`. Confirm against a live commish row.

Single source of truth for two pieces of league history that the pipeline does
**not** currently preserve in full: the complete chronological transaction/event
log, and the team/owner avatars. The handoff prompt in §6 lets a fresh session
pick this up cold.

## 0. Decisions driving this plan (locked with the user)

1. **Images: bytes on disk + metadata in DB.** Download each avatar's bytes to a
   content-addressed blob store under `data/assets/` (gitignored) and record
   `sha256` / `source_url` / `content_type` / `storage_path` in an `assets`
   table. NFL.com CDN assets for a legacy league will eventually rot, so a
   URL-only capture does not actually preserve anything. Dedup identical default
   avatars by `sha256`. Keep raw bytes out of the DB (portable to Postgres,
   keeps the `.db` small).
2. **Event scope: all of the above.** Capture player moves
   (add/drop/waiver/trade) **and** lineup/start-sit changes **and**
   league/setting changes as one chronological league diary. This is broader
   than today's intended scope and needs a generic payload column on
   `transactions`.

## 1. What the audit found

### 1a. The transaction log is paginated but only page 1 is fetched

There *is* a transactions path and the parser is good — but the historical log
is badly incomplete.

- Fetch site: `src/ff_pipeline/crawlers/nfl_com/league.py:208-219` does a single
  `fetcher.get_html(transactions(league_id, year))` with **no pagination loop**.
- Parser `parse_transactions` (`crawlers/nfl_com/parsers.py:936`) correctly
  handles add / drop / waiver / trade / IR, emits two legs per trade, and pulls
  rich fields (timestamp, effective week, direction, the "By" owner note).
- Upsert `_upsert_transactions` (`crawlers/nfl_com/league.py:693`) is idempotent
  via a `(type, team, player, direction, executed_at)` fingerprint.

**Evidence of the gap** (current `data/fantasy.db`): every season has 180 draft
picks (complete) but only **2–8 adds/drops, zero trades ever**, and every
non-draft row is clustered in **late December (weeks 16–17)** — i.e. only the
most-recent page landed. September–November moves and every trade in league
history are missing. The parser is fine; the *fetch* is one page deep.

**Pagination pattern to mirror:** `crawlers/nfl_com/availability.py`
(`sweep_availability`) already walks `offset=0,25,50,…` until no next page, and
`parse_availability_page` extracts `next_offset` from NFL.com's shared
pagination widget. The transactions page uses the same widget, so the
next-offset detection is reusable.

### 1b. Lineup + league/setting events are excluded by design

`parse_transactions` skips `_TXN_TYPES_TO_SKIP = {"lineup", "starter swap"}`
(`parsers.py:927`) and has no concept of league/setting events. Per Decision 2
these must now be **captured**, not skipped.

### 1c. Avatars are parsed past and thrown away

The parsers already touch the `<img>` tags but read only `alt` (for the team
name) and discard `src`, which is the avatar URL:
- `parse_owners` — `crawlers/nfl_com/parsers.py:395-423`
- standings/team-name img read — `parsers.py:580` and `parsers.py:1490`

No image/asset handling exists anywhere in the codebase. Team logos and owner
avatars on the **per-season** standings/owners pages are the icons to preserve
*as they appeared each season*.

### 1d. What is already solid — do not redo

Per-season standings (names, finish order, champion/runner-up/last, medal
records) via `reconstruct_standings`, and per-season manager identities via
`reconstruct_owners` (`crawlers/nfl_com/history.py`). Draft capture is complete.

## 2. Workstream A — complete the chronological event log

Dependency: A is independent of B. Do A first (smaller, higher value).

1. **Confirm pagination param against a live page** (cookie is in `.env`). It
   almost certainly mirrors the players page's `&offset=` plus the same widget.
   Add a `transactions(..., offset=0)` variant to `crawlers/nfl_com/urls.py`
   (today it's param-less at `urls.py:83`).
2. **Add `sweep_transactions`** modeled on `sweep_availability`: walk offsets,
   dedupe across pages by the NFL.com txn id (`_TXN_ID_FROM_CLASS`,
   `parsers.py:931`), stop on no-next-offset. Make it fixture-testable with a
   canned multi-page stub client (mirror the availability sweep tests).
3. **Widen `parse_transactions` to the full scope** (Decision 2):
   - Stop skipping lineup rows; map them to a `lineup_change` type with the
     slot move in the payload (from/to slot, player, direction in=to-starter /
     out=to-bench).
   - Recognize league/setting-change rows → a `setting_change` /
     `league_change` type; these may have no player and sometimes no team.
4. **Schema: add `transactions.extra_data` JSON** (mirror
   `team_rosters.extra_data` in `repository/models.py`) to hold the
   lineup-slot detail and the setting-change payload — `transactions` has no
   JSON column today. Alembic migration.
5. **Swap `league.py:209`** to use `sweep_transactions`. The existing
   fingerprint upsert backfills the missing rows without duplicating the
   December rows already stored. Trade legs already stitch via
   `nfl_transaction_id` → `counterpart_team_id` (verify this still works across
   pages; the stitch currently assumes one page — it may need to key on the
   shared txn id across the whole sweep).
6. **Re-run** the per-season scrape across all seasons; verify add/drop/trade
   counts jump to realistic numbers and the earliest non-draft `executed_at`
   per season moves back into September.

## 3. Workstream B — preserve team/owner avatars

1. **`assets` table** (new, in `repository/models.py`) — content-addressed:
   `asset_id` PK, `league_id` FK, `kind` (`team_avatar` | `user_avatar`),
   `source_url`, `sha256` (UNIQUE — dedup), `content_type`, `byte_size`,
   `storage_path`, `fetched_at`, `created_at`/`updated_at`. Alembic migration.
2. **Keep the `src` the parsers discard:** add `team_logo_url` to
   `ParsedStandingEntry` / `ParsedOwner` and `owner_avatar_url` to
   `ParsedOwner` (read `img["src"]` next to the existing `img["alt"]` reads at
   `parsers.py:580`, `:1490`, and in `parse_owners`).
3. **Downloader** (new module, e.g. `crawlers/nfl_com/media.py`): fetch each URL
   once, write bytes to `data/assets/<sha256[:2]>/<sha256>.<ext>`, insert the
   `assets` row (skip if `sha256` already present). Gitignore `data/assets/`.
   Reuse the authenticated `NflComClient`; add a `get_bytes` if it only does
   `get_html` today.
4. **Link per-season:** add `team_avatar_asset_id` + `owner_avatar_asset_id`
   FKs on `teams` (`teams` is already a per-season row, so this snapshots the
   logo and owner avatar as they appeared that season). Alembic migration.
5. **Wire into reconstruct:** populate the URLs in `reconstruct_standings` /
   `reconstruct_owners` (`history.py`), then run the downloader to fill
   `assets` + the `teams` FKs.
6. **Read API:** expose avatar refs on the team/owner responses
   (`api/routes/teams.py`, `owners.py`) — a URL that streams the stored bytes,
   or the `storage_path`. Confirm the contract in `docs/06_API_CONTRACT.md`.

## 4. Migrations & data-model docs

- Three schema changes: `transactions.extra_data` (A4), `assets` table (B1),
  `teams.{team_avatar,owner_avatar}_asset_id` (B4). One Alembic revision per
  logical change, chained on the current head
  (`f7a2b4c8d1e3_add_players_rostered_season_span` is the latest under
  `alembic/versions/`). Migrate up before any backfill (see memory:
  backfill-operational-gotchas).
- Update `docs/04_DATA_MODEL.md` and `docs/03_DATA_SOURCES.md`.

## 5. Verification checklist

- [ ] `sweep_transactions` unit test passes over a canned multi-page fixture.
- [ ] Trade legs stitch `counterpart_team_id` correctly across page boundaries.
- [ ] Post-backfill: realistic per-season add/drop/trade counts; earliest
      non-draft `executed_at` lands in Sept/Oct, not late December.
- [ ] `lineup_change` + `setting_change` rows present with payload in
      `extra_data`.
- [ ] `assets` rows exist; `data/assets/` holds the bytes; identical default
      avatars dedupe to one row by `sha256`.
- [ ] `teams.team_avatar_asset_id` / `owner_avatar_asset_id` populated per
      season; read API surfaces them.
- [ ] Re-runs are idempotent (no duplicate transactions, no re-downloaded
      assets).

## 6. Handoff prompt (paste into a fresh session)

> You are picking up the "League-History Media & Event-Log" work. Read
> `docs/PHASE_LEAGUE_HISTORY_PLAN.md` end to end first — it has the full audit,
> file/line references, and the two locked decisions. You are already on branch
> `feature/league-history-media-and-event-log` (cut from `dev`); if not, cut it
> from `dev`.
>
> Two locked decisions: (1) preserve avatars as **bytes on disk + metadata in a
> new `assets` table** (content-addressed under `data/assets/`, gitignored, dedup
> by sha256 — not URL-only); (2) capture the **broadest** chronological league
> diary — player moves AND lineup/start-sit changes AND league/setting changes.
>
> Do Workstream A (§2) first — it's the higher-value, self-contained piece:
> the transactions page is paginated and only page 1 is being fetched
> (`crawlers/nfl_com/league.py:209` has no pagination loop), so the historical
> log is missing nearly all adds/drops and every trade. Mirror
> `sweep_availability` (`crawlers/nfl_com/availability.py`) to build
> `sweep_transactions`; widen `parse_transactions` to stop skipping lineup rows
> and to recognize setting/league events; add a `transactions.extra_data` JSON
> column for their payloads. Then Workstream B (§3) for the avatars.
>
> Verify against the §5 checklist before opening a PR. Follow the repo git model:
> this is a `feature/*` branch → PR to **`dev`** only, never `main`. Use the
> commit-trailer standard (AI-Model / Prompted-By / Reviewed-By; no
> Co-Authored-By). Migrate up before any backfill; the live scrape needs the
> NFL.com cookie in `.env`.
