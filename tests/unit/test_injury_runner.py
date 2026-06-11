"""Unit tests for the nflverse injury-report crawler and query helper.

No network calls — uses an in-memory SQLite DB seeded with minimal player
rows, and a hand-built Polars frame in place of nflreadpy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from ff_pipeline.crawlers.nflverse.injury_runner import run_injury_reports
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Player, PlayerInjuryReport
from ff_pipeline.repository.queries import injury_reports_for_week

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    from sqlalchemy.orm import Session as _Session

    engine = create_app_engine(f"sqlite:///{tmp_path / 'injury_test.db'}")
    upgrade_to_head(engine=engine)
    with _Session(engine) as ss:
        yield ss
    engine.dispose()


@pytest.fixture
def two_players(session: Session) -> tuple[Player, Player]:
    p1 = Player(name_full="Kirk Cousins", gsis_id="00-0029604", position="QB")
    p2 = Player(name_full="Justin Jefferson", gsis_id="00-0036322", position="WR")
    session.add_all([p1, p2])
    session.flush()
    return p1, p2


def _make_injury_df(
    rows: list[dict[str, object]],
) -> pl.DataFrame:
    schema = {
        "gsis_id": pl.String,
        "season": pl.Int32,
        "week": pl.Int32,
        "game_type": pl.String,
        "report_status": pl.String,
        "report_primary_injury": pl.String,
        "report_secondary_injury": pl.String,
        "practice_status": pl.String,
        "date_modified": pl.String,
    }
    return pl.DataFrame(rows, schema=schema)


class _FakeSource:
    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def load_injuries(self, seasons: object) -> pl.DataFrame:  # noqa: ARG002
        return self._df

    # Protocol stubs — not exercised by these tests
    def load_player_stats(self, seasons: object) -> pl.DataFrame:
        raise NotImplementedError

    def load_players(self) -> pl.DataFrame:
        raise NotImplementedError

    def load_rosters(self, seasons: object) -> pl.DataFrame:
        raise NotImplementedError

    def load_schedules(self, seasons: object) -> pl.DataFrame:
        raise NotImplementedError

    def load_team_stats(self, seasons: object) -> pl.DataFrame:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests: run_injury_reports
# ---------------------------------------------------------------------------


def test_run_inserts_matched_rows(session: Session, two_players: tuple[Player, Player]) -> None:
    p1, p2 = two_players
    df = _make_injury_df(
        [
            {
                "gsis_id": p1.gsis_id,
                "season": 2023,
                "week": 5,
                "game_type": "REG",
                "report_status": "Out",
                "report_primary_injury": "Knee",
                "report_secondary_injury": None,
                "practice_status": "Did Not Practice",
                "date_modified": None,
            },
            {
                "gsis_id": p2.gsis_id,
                "season": 2023,
                "week": 5,
                "game_type": "REG",
                "report_status": "Questionable",
                "report_primary_injury": "Hamstring",
                "report_secondary_injury": None,
                "practice_status": "Limited Participation In Practice",
                "date_modified": None,
            },
        ]
    )
    result = run_injury_reports(session, seasons=[2023], source=_FakeSource(df))
    assert result.rows_added == 2
    assert result.rows_updated == 0

    reports = session.query(PlayerInjuryReport).all()
    by_pid = {r.player_id: r for r in reports}
    assert by_pid[p1.player_id].report_status == "Out"
    assert by_pid[p1.player_id].report_primary_injury == "Knee"
    assert by_pid[p2.player_id].report_status == "Questionable"


def test_run_skips_null_gsis_id(session: Session, two_players: tuple[Player, Player]) -> None:
    p1, _ = two_players
    df = _make_injury_df(
        [
            {
                "gsis_id": None,  # no gsis_id — should be skipped
                "season": 2023,
                "week": 3,
                "game_type": "REG",
                "report_status": "Out",
                "report_primary_injury": "Ankle",
                "report_secondary_injury": None,
                "practice_status": None,
                "date_modified": None,
            },
            {
                "gsis_id": p1.gsis_id,
                "season": 2023,
                "week": 3,
                "game_type": "REG",
                "report_status": "Doubtful",
                "report_primary_injury": "Back",
                "report_secondary_injury": None,
                "practice_status": None,
                "date_modified": None,
            },
        ]
    )
    result = run_injury_reports(session, seasons=[2023], source=_FakeSource(df))
    assert result.rows_added == 1


def test_run_skips_unresolvable_gsis_id(session: Session) -> None:
    """A gsis_id with no matching player row is silently dropped."""
    df = _make_injury_df(
        [
            {
                "gsis_id": "00-UNKNOWN",
                "season": 2023,
                "week": 1,
                "game_type": "REG",
                "report_status": "Out",
                "report_primary_injury": "Knee",
                "report_secondary_injury": None,
                "practice_status": None,
                "date_modified": None,
            }
        ]
    )
    result = run_injury_reports(session, seasons=[2023], source=_FakeSource(df))
    assert result.rows_added == 0
    assert session.query(PlayerInjuryReport).count() == 0


def test_run_upserts_on_rerun(session: Session, two_players: tuple[Player, Player]) -> None:
    p1, _ = two_players
    df_first = _make_injury_df(
        [
            {
                "gsis_id": p1.gsis_id,
                "season": 2023,
                "week": 7,
                "game_type": "REG",
                "report_status": "Questionable",
                "report_primary_injury": "Knee",
                "report_secondary_injury": None,
                "practice_status": None,
                "date_modified": None,
            }
        ]
    )
    run_injury_reports(session, seasons=[2023], source=_FakeSource(df_first))
    session.flush()

    # Status upgraded to Out — re-run should update, not duplicate.
    df_second = _make_injury_df(
        [
            {
                "gsis_id": p1.gsis_id,
                "season": 2023,
                "week": 7,
                "game_type": "REG",
                "report_status": "Out",
                "report_primary_injury": "Knee",
                "report_secondary_injury": None,
                "practice_status": None,
                "date_modified": None,
            }
        ]
    )
    result2 = run_injury_reports(session, seasons=[2023], source=_FakeSource(df_second))
    assert result2.rows_updated == 1
    assert result2.rows_added == 0
    assert session.query(PlayerInjuryReport).count() == 1
    report = session.query(PlayerInjuryReport).one()
    assert report.report_status == "Out"


# ---------------------------------------------------------------------------
# Tests: injury_reports_for_week query
# ---------------------------------------------------------------------------


def test_injury_reports_for_week_returns_correct_mapping(
    session: Session, two_players: tuple[Player, Player]
) -> None:
    p1, p2 = two_players
    session.add(
        PlayerInjuryReport(
            player_id=p1.player_id,
            season_year=2022,
            week=10,
            game_type="REG",
            report_status="Out",
        )
    )
    session.add(
        PlayerInjuryReport(
            player_id=p2.player_id,
            season_year=2022,
            week=10,
            game_type="REG",
            report_status="Probable",
        )
    )
    # Different week — should not appear.
    session.add(
        PlayerInjuryReport(
            player_id=p1.player_id,
            season_year=2022,
            week=11,
            game_type="REG",
            report_status="Questionable",
        )
    )
    session.flush()

    result = injury_reports_for_week(session, season_year=2022, week=10)
    assert set(result.keys()) == {p1.player_id, p2.player_id}
    assert result[p1.player_id].report_status == "Out"
    assert result[p2.player_id].report_status == "Probable"


def test_injury_reports_for_week_empty_when_none(session: Session) -> None:
    result = injury_reports_for_week(session, season_year=2022, week=1)
    assert result == {}
