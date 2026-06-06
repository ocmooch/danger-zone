"""Integration tests for the team/owner avatar downloader + backfill."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.crawlers.nfl_com.media import (
    backfill_team_avatars,
    download_and_store,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Asset, League, Owner, Season, Team

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "nfl_com_html"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class _Stub:
    """Serves the owners page for HTML and deterministic bytes per URL."""

    def __init__(self, bytes_by_url: dict[str, bytes] | None = None) -> None:
        self.html_calls: list[str] = []
        self.bytes_calls: list[str] = []
        self._bytes_by_url = bytes_by_url or {}

    def get_html(self, url: str) -> str:
        self.html_calls.append(url)
        assert "owners" in url
        return _load("owners.html")

    def get_bytes(self, url: str) -> tuple[bytes, str | None]:
        self.bytes_calls.append(url)
        data = self._bytes_by_url.get(url, b"bytes:" + url.encode())
        return data, "image/jpeg"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed(session: Session, *, year: int = 2024, n_teams: int = 12) -> int:
    session.add(League(league_id="36271", name="The Danger Zone", platform="nfl_com"))
    season = Season(league_id="36271", year=year, status="completed")
    session.add(season)
    session.flush()
    for nfl_team_id in range(1, n_teams + 1):
        owner = Owner(league_id="36271", display_name=f"owner{nfl_team_id}", is_active=True)
        session.add(owner)
        session.flush()
        session.add(
            Team(
                season_id=season.season_id,
                owner_id=owner.owner_id,
                team_name=f"team {nfl_team_id}",
                team_abbrev=str(nfl_team_id),
            )
        )
    session.flush()
    return season.season_id


def test_download_and_store_is_content_addressed(session: Session, tmp_path: Path) -> None:
    session.add(League(league_id="36271", name="x", platform="nfl_com"))
    session.flush()
    root = tmp_path / "assets"
    stub = _Stub({"https://cdn/a.jpg": b"\x89PNGdata"})

    asset_id = download_and_store(
        session, stub, league_id="36271", kind="team_avatar",
        source_url="https://cdn/a.jpg", assets_root=root,
    )
    assert asset_id is not None
    asset = session.get(Asset, asset_id)
    assert asset is not None
    # Bytes live on disk under <sha[:2]>/<sha>.<ext>; only metadata in DB.
    on_disk = root / asset.storage_path
    assert on_disk.exists()
    assert on_disk.read_bytes() == b"\x89PNGdata"
    assert asset.storage_path.startswith(asset.sha256[:2] + "/")
    assert asset.byte_size == len(b"\x89PNGdata")

    # Same URL again → no second network call, same row.
    again = download_and_store(
        session, stub, league_id="36271", kind="team_avatar",
        source_url="https://cdn/a.jpg", assets_root=root,
    )
    assert again == asset_id
    assert stub.bytes_calls == ["https://cdn/a.jpg"]  # fetched exactly once


def test_download_and_store_dedupes_identical_bytes_by_sha(session: Session, tmp_path: Path) -> None:
    session.add(League(league_id="36271", name="x", platform="nfl_com"))
    session.flush()
    root = tmp_path / "assets"
    # Two different URLs (e.g. two teams' default avatar) → identical bytes.
    stub = _Stub({"https://cdn/a.jpg": b"DEFAULT", "https://cdn/b.jpg": b"DEFAULT"})

    a = download_and_store(
        session, stub, league_id="36271", kind="team_avatar",
        source_url="https://cdn/a.jpg", assets_root=root,
    )
    b = download_and_store(
        session, stub, league_id="36271", kind="team_avatar",
        source_url="https://cdn/b.jpg", assets_root=root,
    )
    assert a == b  # deduped onto a single content-addressed row
    assert (session.scalar(select(func.count()).select_from(Asset))) == 1


def test_backfill_links_every_team_and_is_idempotent(session: Session, tmp_path: Path) -> None:
    _seed(session)
    root = tmp_path / "assets"
    stub = _Stub()

    result = backfill_team_avatars(session, stub, league_id="36271", assets_root=root)
    session.commit()

    # owners.html carries 12 distinct logo URLs → 12 assets, 12 teams linked.
    assert result.seasons_processed == 1
    assert result.assets_stored == 12
    assert result.teams_linked == 12
    assert session.scalar(select(func.count()).select_from(Asset)) == 12
    linked = session.execute(
        select(func.count()).select_from(Team).where(Team.team_avatar_asset_id.is_not(None))
    ).scalar_one()
    assert linked == 12
    # Every stored asset's bytes exist on disk.
    for asset in session.execute(select(Asset)).scalars():
        assert (root / asset.storage_path).exists()

    # Re-run: nothing new downloaded, nothing relinked.
    bytes_calls_before = len(stub.bytes_calls)
    result2 = backfill_team_avatars(session, stub, league_id="36271", assets_root=root)
    session.commit()
    assert result2.assets_stored == 0
    assert result2.teams_linked == 0
    assert len(stub.bytes_calls) == bytes_calls_before  # no re-download
    assert session.scalar(select(func.count()).select_from(Asset)) == 12
