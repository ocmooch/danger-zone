"""TestClient coverage for the avatar asset endpoint + TeamOut surfacing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ff_pipeline.api.main import create_app
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Asset, League, Owner, Season, Team

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'api.db'}")
    upgrade_to_head(engine=engine)
    assets_root = tmp_path / "assets"

    with Session(engine) as ss:
        ss.add(League(league_id="L1", name="L", platform="nfl_com"))
        season = Season(league_id="L1", year=2024, status="completed")
        ss.add(season)
        ss.flush()
        owner = Owner(league_id="L1", display_name="o", is_active=True)
        ss.add(owner)
        ss.flush()
        # A stored asset whose bytes live on disk under the content-addressed path.
        sha = "ab" + "c" * 62
        rel = f"{sha[:2]}/{sha}.jpg"
        (assets_root / sha[:2]).mkdir(parents=True, exist_ok=True)
        (assets_root / rel).write_bytes(b"LOGOBYTES")
        asset = Asset(
            league_id="L1",
            kind="team_avatar",
            source_url="https://cdn/x.jpg",
            sha256=sha,
            content_type="image/jpeg",
            byte_size=9,
            storage_path=rel,
        )
        ss.add(asset)
        ss.flush()
        team = Team(
            season_id=season.season_id,
            owner_id=owner.owner_id,
            team_name="T",
            team_abbrev="1",
            team_avatar_asset_id=asset.asset_id,
        )
        ss.add(team)
        ss.commit()
        asset_id = asset.asset_id
        team_id = team.team_id

    app = create_app(engine=engine, assets_dir=assets_root)
    test_client = TestClient(app)
    test_client.asset_id = asset_id  # type: ignore[attr-defined]
    test_client.team_id = team_id  # type: ignore[attr-defined]
    return test_client


def test_team_response_surfaces_avatar_asset_id(client: TestClient) -> None:
    resp = client.get(f"/teams/{client.team_id}")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["team_avatar_asset_id"] == client.asset_id  # type: ignore[attr-defined]
    assert data["owner_avatar_asset_id"] is None


def test_assets_endpoint_streams_stored_bytes(client: TestClient) -> None:
    resp = client.get(f"/assets/{client.asset_id}")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert resp.content == b"LOGOBYTES"
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_assets_endpoint_404_for_unknown_id(client: TestClient) -> None:
    assert client.get("/assets/999999").status_code == 404
