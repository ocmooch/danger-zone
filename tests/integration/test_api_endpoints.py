"""TestClient-based coverage of every documented FastAPI endpoint.

The fixture seeds a temp SQLite database with:

* one league (id ``"L1"``) carrying two seasons (2024 + 2025);
* twelve owners and one team per owner per season (so 24 teams total);
* a single matchup in 2025 week 1 between teams 1 & 2;
* one transaction, two players (one rostered, one free agent);
* raw + scored stats for the rostered player in 2025 week 1;
* a projection + availability rows + one trending row;
* a scoring rule set on both seasons (with one rule modified in 2025);
* one pipeline run with source-health rows so ``/status`` has content.

Each test hits a specific endpoint and asserts envelope shape + key data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ff_pipeline.api.main import create_app
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import (
    League,
    Matchup,
    Owner,
    PipelineRun,
    Player,
    PlayerAvailability,
    PlayerStatsRaw,
    PlayerStatsScored,
    Projection,
    ScoringRule,
    Season,
    SourceHealth,
    Team,
    TeamRoster,
    Transaction,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture: a fully-populated DB + TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'api.db'}"
    engine = create_app_engine(url)
    upgrade_to_head(engine=engine)
    return engine


@pytest.fixture
def seeded_db(db_engine: Engine) -> Engine:
    """Populate the temp DB with a coherent fixture."""
    now = datetime.now(UTC)
    with Session(db_engine) as ss:
        league = League(
            league_id="L1",
            name="Test League",
            platform="nfl_com",
            current_season_year=2025,
        )
        ss.add(league)
        ss.flush()

        season_2024 = Season(
            league_id="L1",
            year=2024,
            status="completed",
            regular_season_weeks=14,
            playoff_weeks=3,
        )
        season_2025 = Season(
            league_id="L1",
            year=2025,
            status="in_progress",
            regular_season_weeks=14,
            playoff_weeks=3,
        )
        ss.add_all([season_2024, season_2025])
        ss.flush()

        owners = [
            Owner(
                league_id="L1",
                display_name=f"Owner {i}",
                nfl_user_id=str(1000 + i),
                is_active=True,
                joined_year=2024,
            )
            for i in range(1, 3)
        ]
        ss.add_all(owners)
        ss.flush()

        teams_2024 = [
            Team(
                season_id=season_2024.season_id,
                owner_id=owners[0].owner_id,
                team_name="Alpha 2024",
                draft_position=1,
                final_rank=1,
                regular_season_wins=10,
                regular_season_losses=4,
                regular_season_ties=0,
                regular_season_points_for=1600.5,
                regular_season_points_against=1400.2,
                made_playoffs=True,
                playoff_finish=1,
            ),
            Team(
                season_id=season_2024.season_id,
                owner_id=owners[1].owner_id,
                team_name="Bravo 2024",
                draft_position=2,
                final_rank=2,
                regular_season_wins=8,
                regular_season_losses=6,
                regular_season_ties=0,
                regular_season_points_for=1500.0,
                regular_season_points_against=1450.0,
                made_playoffs=True,
                playoff_finish=2,
            ),
        ]
        teams_2025 = [
            Team(
                season_id=season_2025.season_id,
                owner_id=owners[0].owner_id,
                team_name="Alpha 2025",
                draft_position=1,
            ),
            Team(
                season_id=season_2025.season_id,
                owner_id=owners[1].owner_id,
                team_name="Bravo 2025",
                draft_position=2,
            ),
        ]
        ss.add_all(teams_2024 + teams_2025)
        ss.flush()

        # Crown a 2024 champion so the aggregate route exercises that path.
        season_2024.champion_team_id = teams_2024[0].team_id
        season_2024.runner_up_team_id = teams_2024[1].team_id

        # Players: one rostered (with stats), one free agent.
        player_rostered = Player(
            name_full="Lamar Jackson",
            name_first="Lamar",
            name_last="Jackson",
            position="QB",
            nfl_team="BAL",
            is_active=True,
            gsis_id="00-0034796",
            sleeper_id="4881",
        )
        player_fa = Player(
            name_full="Roman Wilson",
            name_first="Roman",
            name_last="Wilson",
            position="WR",
            nfl_team="PIT",
            is_active=True,
        )
        ss.add_all([player_rostered, player_fa])
        ss.flush()

        roster_row = TeamRoster(
            team_id=teams_2025[0].team_id,
            player_id=player_rostered.player_id,
            season_year=2025,
            week=1,
            roster_slot="QB",
            is_starter=True,
            acquisition_type="draft",
            acquisition_week=0,
            acquisition_date=now,
        )
        ss.add(roster_row)

        # A matchup in week 1 of 2025 between team[0] (winner) and team[1].
        matchup = Matchup(
            season_id=season_2025.season_id,
            week=1,
            team_id=teams_2025[0].team_id,
            opponent_team_id=teams_2025[1].team_id,
            team_score=124.5,
            opponent_score=98.1,
            is_win=True,
            is_playoff=False,
            is_consolation=False,
        )
        ss.add(matchup)

        # Mirror matchup for losing side so standings_for_season produces both rows.
        ss.add(
            Matchup(
                season_id=season_2025.season_id,
                week=1,
                team_id=teams_2025[1].team_id,
                opponent_team_id=teams_2025[0].team_id,
                team_score=98.1,
                opponent_score=124.5,
                is_win=False,
                is_playoff=False,
            )
        )

        ss.add(
            Transaction(
                season_id=season_2025.season_id,
                transaction_type="add",
                executed_at=now,
                effective_week=1,
                team_id=teams_2025[0].team_id,
                player_id=player_fa.player_id,
                direction="in",
            )
        )

        raw = PlayerStatsRaw(
            player_id=player_rostered.player_id,
            season_year=2025,
            week=1,
            source="nflverse",
            stats={"passing_yards": 312, "passing_tds": 3, "rushing_yards": 41},
            is_primary=True,
            ingested_at=now,
        )
        ss.add(raw)
        ss.flush()
        ss.add(
            PlayerStatsScored(
                stat_id=raw.stat_id,
                season_id=season_2025.season_id,
                player_id=player_rostered.player_id,
                week=1,
                total_points=29.78,
                points_breakdown={"passing": 20.48, "rushing": 5.10, "bonus": 4.2},
            )
        )

        ss.add(
            Projection(
                player_id=player_rostered.player_id,
                season_year=2025,
                week=1,
                source="sleeper",
                projected_points=24.5,
                projected_stats={"passing_yards": 280},
                fetched_at=now,
            )
        )

        ss.add_all(
            [
                PlayerAvailability(
                    player_id=player_rostered.player_id,
                    season_year=2025,
                    week=1,
                    status="OWNED",
                    owning_team_id=teams_2025[0].team_id,
                    is_pre_kickoff_snapshot=True,
                ),
                PlayerAvailability(
                    player_id=player_fa.player_id,
                    season_year=2025,
                    week=1,
                    status="FREE_AGENT",
                    is_pre_kickoff_snapshot=True,
                ),
            ]
        )

        # Scoring rules: same set in both seasons, but one rule (passing_tds)
        # changes value year-over-year so the diff route has something to show.
        for season_id, td_value in [(season_2024.season_id, 4.0), (season_2025.season_id, 6.0)]:
            ss.add_all(
                [
                    ScoringRule(
                        season_id=season_id,
                        category="passing",
                        stat_key="passing_yards",
                        points_per_unit=0.04,
                        unit_size=1.0,
                    ),
                    ScoringRule(
                        season_id=season_id,
                        category="passing",
                        stat_key="passing_tds",
                        points_per_unit=td_value,
                        unit_size=1.0,
                    ),
                ]
            )
        # Plus a rule only on 2025 to exercise "added" diff branch
        ss.add(
            ScoringRule(
                season_id=season_2025.season_id,
                category="bonus",
                stat_key="big_passing_game",
                flat_points=3.0,
                threshold_min=300.0,
            )
        )

        run = PipelineRun(
            started_at=now,
            finished_at=now,
            status="success",
            mode="nfl_com_league",
        )
        ss.add(run)
        ss.flush()
        ss.add(
            SourceHealth(
                run_id=run.run_id,
                source="nfl_com",
                status="success",
                rows_added=10,
                rows_updated=2,
                duration_ms=1200,
            )
        )
        ss.commit()
    return db_engine


@pytest.fixture
def client(seeded_db: Engine) -> TestClient:
    return TestClient(create_app(engine=seeded_db))


@pytest.fixture
def empty_client(db_engine: Engine) -> TestClient:
    """Client backed by an empty (migrated but unseeded) DB."""
    return TestClient(create_app(engine=db_engine))


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_status_has_pipeline_summary(client: TestClient) -> None:
    body = client.get("/status").json()
    assert body["data"]["last_run_status"] == "success"
    assert body["data"]["sources"][0]["source"] == "nfl_com"
    assert body["meta"]["pipeline_run_id"] is not None


def test_status_works_on_empty_db(empty_client: TestClient) -> None:
    body = empty_client.get("/status").json()
    assert body["data"]["last_run_id"] is None
    assert body["meta"]["pipeline_run_id"] is None


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------


def test_list_leagues(client: TestClient) -> None:
    body = client.get("/leagues").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["league_id"] == "L1"
    assert body["data"][0]["season_count"] == 2
    assert body["data"][0]["owner_count"] == 2


def test_get_league(client: TestClient) -> None:
    body = client.get("/leagues/L1").json()
    assert body["data"]["league_id"] == "L1"
    assert body["data"]["name"] == "Test League"


def test_get_league_404(client: TestClient) -> None:
    resp = client.get("/leagues/nope")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_league_owners(client: TestClient) -> None:
    body = client.get("/leagues/L1/owners").json()
    assert len(body["data"]) == 2
    assert {o["display_name"] for o in body["data"]} == {"Owner 1", "Owner 2"}


def test_league_owners_404(client: TestClient) -> None:
    assert client.get("/leagues/nope/owners").status_code == 404


def test_league_seasons(client: TestClient) -> None:
    body = client.get("/leagues/L1/seasons").json()
    assert [s["year"] for s in body["data"]] == [2024, 2025]


def test_league_seasons_404(client: TestClient) -> None:
    assert client.get("/leagues/nope/seasons").status_code == 404


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------


def test_get_season(client: TestClient) -> None:
    body = client.get("/seasons/1").json()
    assert body["data"]["year"] == 2024
    assert body["data"]["status"] == "completed"


def test_get_season_404(client: TestClient) -> None:
    assert client.get("/seasons/9999").status_code == 404


def test_season_standings(client: TestClient) -> None:
    body = client.get("/seasons/2/standings").json()
    rows = body["data"]["rows"]
    assert len(rows) == 2
    # Team 3 (Alpha 2025) is the winner — should sort first.
    assert rows[0]["wins"] == 1
    assert rows[0]["points_for"] == 124.5


def test_season_standings_with_through_week(client: TestClient) -> None:
    body = client.get("/seasons/2/standings?through_week=1").json()
    assert body["data"]["through_week"] == 1
    assert len(body["data"]["rows"]) == 2


def test_season_standings_404(client: TestClient) -> None:
    assert client.get("/seasons/9999/standings").status_code == 404


def test_season_teams(client: TestClient) -> None:
    body = client.get("/seasons/1/teams").json()
    assert len(body["data"]) == 2


def test_season_teams_404(client: TestClient) -> None:
    assert client.get("/seasons/9999/teams").status_code == 404


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def test_get_team(client: TestClient) -> None:
    body = client.get("/teams/1").json()
    assert body["data"]["team_name"] == "Alpha 2024"


def test_get_team_404(client: TestClient) -> None:
    assert client.get("/teams/9999").status_code == 404


def test_team_roster_default_week(client: TestClient) -> None:
    body = client.get("/teams/3/roster").json()
    assert body["data"]["week"] == 1
    assert body["data"]["slots"][0]["player"]["name_full"] == "Lamar Jackson"
    assert body["data"]["slots"][0]["acquisition_type"] == "draft"


def test_team_roster_specific_week(client: TestClient) -> None:
    body = client.get("/teams/3/roster?week=1").json()
    assert body["data"]["week"] == 1


def test_team_roster_empty(client: TestClient) -> None:
    # Team 1 has no roster rows in our fixture (2024).
    body = client.get("/teams/1/roster").json()
    assert body["data"]["slots"] == []


def test_team_roster_404(client: TestClient) -> None:
    assert client.get("/teams/9999/roster").status_code == 404


def test_team_matchups(client: TestClient) -> None:
    body = client.get("/teams/3/matchups").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["team_score"] == 124.5


def test_team_matchups_404(client: TestClient) -> None:
    assert client.get("/teams/9999/matchups").status_code == 404


def test_team_transactions(client: TestClient) -> None:
    body = client.get("/teams/3/transactions").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["transaction_type"] == "add"


def test_team_transactions_404(client: TestClient) -> None:
    assert client.get("/teams/9999/transactions").status_code == 404


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------


def test_get_owner(client: TestClient) -> None:
    body = client.get("/owners/1").json()
    assert body["data"]["display_name"] == "Owner 1"


def test_get_owner_404(client: TestClient) -> None:
    assert client.get("/owners/9999").status_code == 404


def test_owner_history(client: TestClient) -> None:
    body = client.get("/owners/1/history").json()
    assert len(body["data"]["seasons"]) == 2
    years = {s["season_year"] for s in body["data"]["seasons"]}
    assert years == {2024, 2025}


def test_owner_history_404(client: TestClient) -> None:
    assert client.get("/owners/9999/history").status_code == 404


def test_owner_aggregate(client: TestClient) -> None:
    body = client.get("/owners/1/aggregate").json()
    assert body["data"]["championships"] == 1
    assert body["data"]["total_wins"] == 10  # only 2024 has W/L populated


def test_owner_aggregate_404(client: TestClient) -> None:
    assert client.get("/owners/9999/aggregate").status_code == 404


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def test_list_players(client: TestClient) -> None:
    body = client.get("/players").json()
    assert len(body["data"]) == 2


def test_list_players_filter_position(client: TestClient) -> None:
    body = client.get("/players?position=QB").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["position"] == "QB"


def test_list_players_name_search(client: TestClient) -> None:
    body = client.get("/players?name=jack").json()
    assert len(body["data"]) == 1


def test_list_players_filter_nfl_team_and_active(client: TestClient) -> None:
    body = client.get("/players?nfl_team=BAL&active=true").json()
    assert len(body["data"]) == 1


def test_get_player(client: TestClient) -> None:
    body = client.get("/players/1").json()
    assert body["data"]["name_full"] == "Lamar Jackson"
    assert body["data"]["gsis_id"] == "00-0034796"


def test_get_player_404(client: TestClient) -> None:
    assert client.get("/players/9999").status_code == 404


def test_player_stats(client: TestClient) -> None:
    body = client.get("/players/1/stats?season=2025&week=1").json()
    assert body["data"]["league_points"] == 29.78
    assert body["data"]["raw_stats"]["passing_yards"] == 312
    assert body["data"]["all_sources"][0]["source"] == "nflverse"


def test_player_stats_404(client: TestClient) -> None:
    assert client.get("/players/9999/stats?season=2025&week=1").status_code == 404


def test_player_ownership(client: TestClient) -> None:
    body = client.get("/players/1/ownership").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["acquisition_type"] == "draft"


def test_player_ownership_404(client: TestClient) -> None:
    assert client.get("/players/9999/ownership").status_code == 404


def test_player_projections(client: TestClient) -> None:
    body = client.get("/players/1/projections").json()
    assert body["data"][0]["projected_points"] == 24.5


def test_player_projections_filtered(client: TestClient) -> None:
    body = client.get("/players/1/projections?season=2025&week=1").json()
    assert len(body["data"]) == 1


def test_player_projections_404(client: TestClient) -> None:
    assert client.get("/players/9999/projections").status_code == 404


def test_player_availability(client: TestClient) -> None:
    body = client.get("/players/1/availability?season=2025").json()
    assert body["data"][0]["status"] == "OWNED"


def test_player_availability_404(client: TestClient) -> None:
    assert client.get("/players/9999/availability?season=2025").status_code == 404


def test_availability_snapshot(client: TestClient) -> None:
    body = client.get("/players/availability?season=2025&week=1").json()
    assert len(body["data"]) == 2


def test_availability_snapshot_status_filter(client: TestClient) -> None:
    body = client.get("/players/availability?season=2025&week=1&status=FREE_AGENT").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "FREE_AGENT"


def test_availability_timeline(client: TestClient) -> None:
    body = client.get("/players/availability/timeline?player_id=1").json()
    assert len(body["data"]) == 1


def test_availability_timeline_404(client: TestClient) -> None:
    assert client.get("/players/availability/timeline?player_id=9999").status_code == 404


# ---------------------------------------------------------------------------
# Matchups
# ---------------------------------------------------------------------------


def test_list_matchups(client: TestClient) -> None:
    body = client.get("/matchups").json()
    assert len(body["data"]) == 2  # both sides of the same matchup


def test_list_matchups_filtered(client: TestClient) -> None:
    body = client.get("/matchups?season=2025&week=1").json()
    assert len(body["data"]) == 2


def test_get_matchup(client: TestClient) -> None:
    body = client.get("/matchups/1").json()
    assert body["data"]["team_score"] == 124.5


def test_get_matchup_404(client: TestClient) -> None:
    assert client.get("/matchups/9999").status_code == 404


def test_matchup_box_score(client: TestClient) -> None:
    body = client.get("/matchups/1/box-score").json()
    assert body["data"]["season_year"] == 2025
    assert body["data"]["home"]["total_score"] == 124.5
    assert body["data"]["away"]["total_score"] == 98.1
    assert body["data"]["winner_team_id"] == 3
    qb = body["data"]["home"]["lineup"][0]
    assert qb["player_name"] == "Lamar Jackson"
    assert qb["league_points"] == 29.78


def test_matchup_box_score_404(client: TestClient) -> None:
    assert client.get("/matchups/9999/box-score").status_code == 404


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_list_transactions(client: TestClient) -> None:
    body = client.get("/transactions").json()
    assert len(body["data"]) == 1


def test_list_transactions_filtered_by_season(client: TestClient) -> None:
    body = client.get("/transactions?season=2025").json()
    assert len(body["data"]) == 1


def test_list_transactions_filtered_by_team(client: TestClient) -> None:
    body = client.get("/transactions?team_id=3").json()
    assert len(body["data"]) == 1


def test_list_transactions_filtered_by_player(client: TestClient) -> None:
    body = client.get("/transactions?player_id=2").json()
    assert len(body["data"]) == 1


# ---------------------------------------------------------------------------
# Scoring rules
# ---------------------------------------------------------------------------


def test_scoring_rules_for_season(client: TestClient) -> None:
    body = client.get("/leagues/L1/seasons/2025/scoring-rules").json()
    keys = {r["stat_key"] for r in body["data"]}
    assert keys == {"passing_yards", "passing_tds", "big_passing_game"}


def test_scoring_rules_for_season_404(client: TestClient) -> None:
    assert client.get("/leagues/nope/seasons/2025/scoring-rules").status_code == 404
    assert client.get("/leagues/L1/seasons/9999/scoring-rules").status_code == 404


def test_scoring_rules_diff(client: TestClient) -> None:
    body = client.get("/leagues/L1/scoring-rules/diff?from=2024&to=2025").json()
    assert body["data"]["from_year"] == 2024
    assert body["data"]["to_year"] == 2025
    by_change = {(c["change"], c["stat_key"]) for c in body["data"]["changes"]}
    assert ("modified", "passing_tds") in by_change
    assert ("added", "big_passing_game") in by_change


def test_scoring_rules_diff_404(client: TestClient) -> None:
    assert client.get("/leagues/nope/scoring-rules/diff?from=2024&to=2025").status_code == 404
    assert client.get("/leagues/L1/scoring-rules/diff?from=9999&to=2025").status_code == 404


# ---------------------------------------------------------------------------
# Stats aggregates
# ---------------------------------------------------------------------------


def test_stats_players_top(client: TestClient) -> None:
    body = client.get("/stats/players/top?season=2025").json()
    assert body["data"][0]["points"] == 29.78


def test_stats_players_top_filtered(client: TestClient) -> None:
    body = client.get("/stats/players/top?season=2025&week=1&position=QB&limit=5").json()
    assert body["data"][0]["points"] == 29.78


def test_stats_season_totals(client: TestClient) -> None:
    body = client.get("/stats/players/season-totals?season=2025").json()
    assert body["data"][0]["total_points"] == 29.78
    assert body["data"][0]["weeks_played"] == 1


def test_stats_owner_career(client: TestClient) -> None:
    body = client.get("/stats/owners/career").json()
    assert len(body["data"]) == 2
    assert body["data"][0]["championships"] == 1


# ---------------------------------------------------------------------------
# Error format + bad input
# ---------------------------------------------------------------------------


def test_bad_request_query_param(client: TestClient) -> None:
    # week=99 violates the ge/le constraint → 400 (validation handler).
    resp = client.get("/teams/3/roster?week=99")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "bad_request"
    assert body["status"] == 400


def test_unknown_path_404_format(client: TestClient) -> None:
    resp = client.get("/this/does/not/exist")
    assert resp.status_code == 404


def test_openapi_docs_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
