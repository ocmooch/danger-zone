# 06 — API Contract

Phases 2 (dashboard) and 3 (decision support) consume Phase 1 through this HTTP API. The contract here is **canonical**: Phase 1 must implement these, and Phase 2/3 must not depend on anything outside this list.

## Conventions

- Base URL: `http://127.0.0.1:8000` (local only, configurable via `API_HOST`/`API_PORT`)
- All endpoints return JSON
- All endpoints are **GET** (read-only)
- All responses include a `meta` object with: `last_updated` (ISO timestamp of underlying data), `source` (which crawler produced this), `pipeline_run_id`
- All endpoints follow REST conventions: `/leagues/{id}/seasons/{year}/...`
- Query parameters use snake_case
- Pagination is offset/limit, default limit 50, max 500
- Errors return `{"error": "...", "detail": "...", "status": 4xx}` with appropriate HTTP status
- OpenAPI docs auto-generated at `GET /docs` (Swagger) and `GET /redoc`

## Endpoints

### Health / status

| Endpoint | Description |
|----------|-------------|
| `GET /health` | `{"status": "ok"}` for liveness |
| `GET /status` | Detailed: last pipeline run, per-source health, any unresolved errors |

### Leagues

| Endpoint | Description |
|----------|-------------|
| `GET /leagues` | List all leagues in the database (will be 1 for Phase 1) |
| `GET /leagues/{league_id}` | League metadata: name, current season, owner count |
| `GET /leagues/{league_id}/owners` | All owners, with active/inactive flag and joined year |
| `GET /leagues/{league_id}/seasons` | List of seasons with status and final winner |

### Scoring rules

| Endpoint | Description |
|----------|-------------|
| `GET /leagues/{league_id}/seasons/{year}/scoring-rules` | Full scoring config for that season |
| `GET /leagues/{league_id}/scoring-rules/diff?from={y1}&to={y2}` | What changed between two seasons |

### Seasons

| Endpoint | Description |
|----------|-------------|
| `GET /seasons/{season_id}` | Season summary: champion, runner-up, final standings |
| `GET /seasons/{season_id}/standings?through_week={n}` | Standings as of week n (current if omitted) |
| `GET /seasons/{season_id}/teams` | All teams that season with owner and final rank |

### Teams

| Endpoint | Description |
|----------|-------------|
| `GET /teams/{team_id}` | Team metadata + season summary |
| `GET /teams/{team_id}/roster?week={n}` | Roster snapshot for that week (default: latest) |
| `GET /teams/{team_id}/matchups` | All matchups for that team that season |
| `GET /teams/{team_id}/transactions` | All transactions involving that team that season |

### Owners

| Endpoint | Description |
|----------|-------------|
| `GET /owners/{owner_id}` | Owner metadata |
| `GET /owners/{owner_id}/history` | All teams across all seasons + season-by-season record |
| `GET /owners/{owner_id}/aggregate` | Career totals: wins, losses, points for, championships, etc. |

### Players

| Endpoint | Description |
|----------|-------------|
| `GET /players` | Searchable index. Query: `?name=`, `?position=`, `?nfl_team=`, `?active=true`, `?league_relevant=true` (only players ever rostered in *this* league; `=false` = never-rostered ghosts), plus exact-match ID lookups `?gsis_id=`, `?sleeper_id=`, `?nfl_com_player_id=` (a player is queryable by any external ID — see M7). Each `PlayerOut` carries `last_season` (last NFL season, nflverse) and the `first_rostered_season` / `last_rostered_season` league span (NULL ⇒ never rostered here) |
| `GET /players/{player_id}` | Player metadata + cross-platform IDs |
| `GET /players/{player_id}/stats?season={y}&week={w}` | Stats (raw + league-adjusted) for the player |
| `GET /players/{player_id}/ownership` | History of which teams have owned this player (and when) |
| `GET /players/{player_id}/projections?season={y}&week={w}` | Latest projections |
| `GET /players/{player_id}/availability?season={y}` | Per-week availability state (OWNED / FREE_AGENT / ON_WAIVERS) across a season |
| `GET /players/availability?season={y}&week={w}&status={s}` | League-wide snapshot: e.g., all free agents at game time of week w |
| `GET /players/availability/timeline?player_id={pid}` | Full history of status changes for a player |

### Matchups

| Endpoint | Description |
|----------|-------------|
| `GET /matchups/{matchup_id}` | Full matchup with both lineups, scores, breakdown |
| `GET /matchups?season={y}&week={w}` | All matchups in a given week |
| `GET /matchups/{matchup_id}/box-score` | Detailed box score with per-player point breakdown |

### Transactions

| Endpoint | Description |
|----------|-------------|
| `GET /transactions?season={y}` | All transactions in a season (paginated) |
| `GET /transactions?team_id={tid}` | All transactions involving a team |
| `GET /transactions?player_id={pid}` | All transactions involving a player |

Transaction rows carry an `extra_data` object for events that don't fit the player-move columns: `lineup_change` rows carry `{"from_slot","to_slot"}`, `setting_change` rows carry the change detail. It is `null` for add/drop/trade rows.

### Assets (avatars)

| Endpoint | Description |
|----------|-------------|
| `GET /assets/{asset_id}` | Streams the stored bytes of a team logo / owner avatar with its recorded `Content-Type` (immutable, long-cache). 404 if the row is unknown or its bytes aren't present on this host. |

`GET /teams/{team_id}` surfaces `team_avatar_asset_id` / `owner_avatar_asset_id`; reference the bytes as `GET /assets/{id}`. Both are `null` until the avatar backfill has run.

### Stats (aggregated)

| Endpoint | Description |
|----------|-------------|
| `GET /stats/players/top?season={y}&week={w}&position={pos}&limit={n}` | Top scorers. `nfl_team` is season-correct — the team the player was on that exact week, not their current team (falls back to the current `players.nfl_team` snapshot when no per-week team is stored) |
| `GET /stats/players/season-totals?season={y}` | Season-long totals per player. `nfl_team` is the player's team of record for that season (modal team, tie-broken by latest week), not their current team |
| `GET /stats/owners/career` | Career stats aggregated by owner |

## Sample responses

### `GET /leagues/{league_id}`

```json
{
  "data": {
    "league_id": "1234567",
    "name": "Friday Night Fantasy",
    "platform": "nfl_com",
    "current_season_year": 2025,
    "season_count": 11,
    "owner_count": 12,
    "created_at": "2025-09-12T03:14:01Z",
    "updated_at": "2025-11-19T08:00:14Z"
  },
  "meta": {
    "last_updated": "2025-11-19T08:00:14Z",
    "source": "nfl_com_league",
    "pipeline_run_id": 142
  }
}
```

### `GET /teams/{team_id}/roster?week=5`

```json
{
  "data": {
    "team_id": 47,
    "team_name": "The Couch GMs",
    "season_year": 2025,
    "week": 5,
    "slots": [
      {
        "roster_slot": "QB",
        "is_starter": true,
        "player": {
          "player_id": 882,
          "name_full": "Lamar Jackson",
          "position": "QB",
          "nfl_team": "BAL"
        },
        "acquisition_type": "draft",
        "acquisition_week": 0
      },
      {
        "roster_slot": "RB1",
        "is_starter": true,
        "player": { "player_id": 901, "name_full": "Bijan Robinson", "position": "RB", "nfl_team": "ATL" },
        "acquisition_type": "draft",
        "acquisition_week": 0
      }
      // ... etc
    ]
  },
  "meta": { ... }
}
```

### `GET /matchups/{matchup_id}/box-score`

```json
{
  "data": {
    "matchup_id": 712,
    "season_year": 2025,
    "week": 5,
    "is_playoff": false,
    "home": {
      "team_id": 47,
      "team_name": "The Couch GMs",
      "owner_name": "Jane Q",
      "total_score": 124.82,
      "lineup": [
        {
          "roster_slot": "QB",
          "player_name": "Lamar Jackson",
          "nfl_opponent": "CIN",
          "nfl_game_status": "Win,27-20",
          "roster_status": null,
          "roster_status_label": null,
          "injury_status": null,
          "injury_primary_injury": null,
          "injury_secondary_injury": null,
          "injury_practice_status": null,
          "reserve_eligibility_status": null,
          "raw_stats": { "passing_yards": 287, "passing_tds": 2, "rushing_yards": 41, "rushing_tds": 1 },
          "league_points": 27.78,
          "breakdown": { "passing": 19.48, "rushing": 10.10, "bonus": 0.0 },
          "status": "played"
        },
        {
          "roster_slot": "BN",
          "player_name": "Some Bye-Week WR",
          "nfl_opponent": null,
          "nfl_game_status": null,
          "roster_status": null,
          "roster_status_label": null,
          "injury_status": null,
          "injury_primary_injury": null,
          "injury_secondary_injury": null,
          "injury_practice_status": null,
          "reserve_eligibility_status": null,
          "raw_stats": {},
          "league_points": null,
          "breakdown": {},
          "status": "bye"
        }
        // ... etc
      ]
    },
    "away": { /* same shape */ },
    "winner_team_id": 47
  },
  "meta": { ... }
}
```

Each lineup entry carries a **`status`** explaining its score so a client can
tell a real zero from a missing one:

| `status` | Meaning | `league_points` |
|----------|---------|-----------------|
| `played` | Player has a scored row; the value is real (it may legitimately be `0.0` or negative). | number |
| `bye` | Player's NFL team had no game that week. | `null` |
| `ir` | Player sat in a reserve/IR roster slot. | `null` |
| `did_not_play` | Player's team played but the player recorded no stats (inactive / scratch), or the franchise was indeterminate. | `null` |

A `null` `league_points` therefore means "no stat data" (`bye`/`ir`/`did_not_play`), **never** "scored zero" — an organic zero is `status: "played"` with `league_points: 0.0`. Bye detection derives the week's playing teams from ingested stats, so for a week that has not been ingested yet every entry falls back to `did_not_play` rather than being mislabeled `bye`.

Lineup entries also carry contextual designation fields:

| Field | Meaning |
|-------|---------|
| `nfl_opponent` / `nfl_game_status` | NFL.com roster snapshot opponent and rendered game result/status, when captured. These distinguish a team bye from a player who simply did not record stats. |
| `roster_status` / `roster_status_label` | NFL.com roster badge captured from the lineup row, e.g. `IR` / `Injured Reserve` or `IA` / `Inactive`. Present only after roster/gamecenter ingestion that captured the badge. |
| `injury_status`, `injury_primary_injury`, `injury_secondary_injury`, `injury_practice_status` | Weekly nflverse injury-report designation/details for the same player and scoring week. Missing fields mean no injury-report row was available, not necessarily healthy. |
| `reserve_eligibility_status` | For `RES`/`IR` fantasy roster slots, the best available eligibility context: roster badge label, roster badge, injury report status/practice status, or the slot name as a final fallback. This can coexist with `status: "played"` when a player remained in a reserve slot after becoming game-active and scoring points. |

### `GET /players/{player_id}/stats?season=2024&week=8`

```json
{
  "data": {
    "player_id": 882,
    "season_year": 2024,
    "week": 8,
    "raw_stats": {
      "source": "nflverse",
      "passing_yards": 312,
      "passing_tds": 3,
      "passing_interceptions": 0,
      "rushing_yards": 51,
      "rushing_tds": 0
    },
    "league_points": 29.78,
    "points_breakdown": {
      "passing": 20.48,
      "rushing": 5.10,
      "bonus": 4.20
    },
    "all_sources": [
      { "source": "nflverse", "stats": { ... } },
      { "source": "nfl_com_api", "stats": { ... } },
      { "source": "sleeper", "stats": { ... } }
    ]
  },
  "meta": { ... }
}
```

## Error format

```json
{
  "error": "not_found",
  "detail": "No team with id 9999 in league 1234567",
  "status": 404
}
```

Common errors:
- `400 bad_request` — invalid query parameter
- `404 not_found` — entity doesn't exist
- `503 service_unavailable` — pipeline has never run successfully

## What's NOT in this API

To keep the contract minimal and focused:

- **No write endpoints**. The database is populated only by the pipeline.
- **No real-time stats** during in-progress games.
- **No analytical computations** (averages, trends, projections beyond simple passthrough). Phase 2/3 compute those.
- **No authentication**. The API binds to `127.0.0.1` only. If we ever expose it, that's a Phase 2 concern.

## Versioning

The first published version is `v1`. If we need breaking changes, we'll mount a `/v2/` prefix in addition to `/v1/`. The OpenAPI doc declares the current version.
