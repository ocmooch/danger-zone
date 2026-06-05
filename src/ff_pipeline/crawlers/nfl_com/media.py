"""Download + content-address team/owner avatars from NFL.com.

Two locked decisions drive this module:

1. Avatars are preserved as **bytes on disk + metadata in the DB**, not as
   bare URLs — NFL.com's CDN assets for a legacy league eventually rot, so a
   URL alone preserves nothing. Bytes land under a content-addressed path
   (``<root>/<sha[:2]>/<sha>.<ext>``); only metadata goes in ``assets``.
2. Identical default avatars across teams **dedupe by sha256** to one row.

The live sync is left untouched; this is a separate, idempotent backfill
(re-runs re-download nothing — a URL already stored short-circuits before
the network, and matching bytes collapse onto the existing row).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, select

from ff_pipeline.crawlers.nfl_com.client import AuthFailureError, NflComClientError
from ff_pipeline.crawlers.nfl_com.parsers import parse_owners
from ff_pipeline.crawlers.nfl_com.urls import history_owners
from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import Asset, Season, Team

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)

# Default content-addressed store. Gitignored (``data/*``); kept out of the
# DB so the SQLite file stays small and portable.
DEFAULT_ASSETS_ROOT = Path("data/assets")

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
_KNOWN_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}


class _AvatarFetcher(Protocol):
    """The slice of ``NflComClient`` the backfill needs (HTML + bytes)."""

    def get_html(self, url: str) -> str: ...
    def get_bytes(self, url: str) -> tuple[bytes, str | None]: ...


@dataclass(frozen=True, slots=True)
class AvatarBackfillResult:
    """Aggregate output of ``backfill_team_avatars``."""

    seasons_processed: int
    assets_stored: int
    teams_linked: int


def _ext_for(source_url: str, content_type: str | None) -> str:
    """Pick a file extension from the URL suffix, else the Content-Type."""
    suffix = Path(urlsplit(source_url).path).suffix.lower()
    if suffix in _KNOWN_IMAGE_EXTS:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        mapped = _CONTENT_TYPE_EXT.get(content_type.split(";", 1)[0].strip().lower())
        if mapped:
            return mapped
    return ".bin"


def download_and_store(
    session: Session,
    fetcher: _AvatarFetcher,
    *,
    league_id: str | None,
    kind: str,
    source_url: str,
    assets_root: Path = DEFAULT_ASSETS_ROOT,
) -> int | None:
    """Fetch one asset, content-address it, and upsert its ``assets`` row.

    Returns the ``asset_id`` (existing or new), or ``None`` if the download
    failed. Idempotent on two levels: an asset already stored under the same
    ``source_url`` short-circuits without a network call, and bytes whose
    ``sha256`` already exists reuse that row instead of writing a duplicate.
    """
    existing = session.scalar(select(Asset).where(Asset.source_url == source_url))
    if existing is not None:
        return existing.asset_id

    try:
        content, content_type = fetcher.get_bytes(source_url)
    except AuthFailureError:
        raise  # dead cookie — abort the whole backfill, don't swallow it
    except NflComClientError as exc:
        log.warning("avatar download failed", url=source_url, error=str(exc))
        return None
    if not content:
        log.warning("avatar download returned no bytes", url=source_url)
        return None

    sha = hashlib.sha256(content).hexdigest()
    dedup = session.scalar(select(Asset).where(Asset.sha256 == sha))
    if dedup is not None:
        return dedup.asset_id

    rel_path = Path(sha[:2]) / f"{sha}{_ext_for(source_url, content_type)}"
    dest = Path(assets_root) / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)

    asset = Asset(
        league_id=league_id,
        kind=kind,
        source_url=source_url,
        sha256=sha,
        content_type=content_type,
        byte_size=len(content),
        # Stored relative to the assets root so the reference survives the
        # store being relocated; the API joins it back to the configured root.
        storage_path=str(rel_path),
        fetched_at=datetime.now(tz=UTC),
    )
    session.add(asset)
    session.flush()
    return asset.asset_id


def _team_by_nfl_id(session: Session, season_id: int) -> dict[int, int]:
    """Map NFL.com team_id (stashed in ``teams.team_abbrev``) → internal id."""
    out: dict[int, int] = {}
    for team_id, abbrev in session.execute(
        select(Team.team_id, Team.team_abbrev).where(Team.season_id == season_id)
    ).all():
        if not abbrev:
            continue
        try:
            out[int(abbrev)] = team_id
        except (TypeError, ValueError):
            continue
    return out


def backfill_team_avatars(
    session: Session,
    fetcher: _AvatarFetcher,
    *,
    league_id: str,
    assets_root: Path = DEFAULT_ASSETS_ROOT,
    years: list[int] | None = None,
) -> AvatarBackfillResult:
    """Snapshot each season's team logos into ``assets`` + ``teams`` FKs.

    Reads the per-season "Managers" page (year-scoped, so the logo is the
    one that season), downloads each team logo once (deduped), and links it
    onto that season's ``teams.team_avatar_asset_id``. Caller commits.
    """
    season_rows = (
        session.execute(
            select(Season).where(Season.league_id == league_id).order_by(Season.year)
        )
        .scalars()
        .all()
    )
    seasons = [s for s in season_rows if years is None or s.year in years]

    assets_before = session.scalar(select(func.count()).select_from(Asset)) or 0
    teams_linked = 0
    for season in seasons:
        parsed = parse_owners(fetcher.get_html(history_owners(league_id, season.year)))
        nfl_to_internal = _team_by_nfl_id(session, season.season_id)
        for owner in parsed:
            if owner.team_id is None or not owner.team_logo_url:
                continue
            internal_id = nfl_to_internal.get(owner.team_id)
            if internal_id is None:
                continue
            asset_id = download_and_store(
                session,
                fetcher,
                league_id=league_id,
                kind="team_avatar",
                source_url=owner.team_logo_url,
                assets_root=assets_root,
            )
            if asset_id is None:
                continue
            team = session.get(Team, internal_id)
            if team is not None and team.team_avatar_asset_id != asset_id:
                team.team_avatar_asset_id = asset_id
                teams_linked += 1

    session.flush()
    assets_after = session.scalar(select(func.count()).select_from(Asset)) or 0
    assets_stored = assets_after - assets_before

    log.info(
        "Backfilled team avatars",
        league_id=league_id,
        seasons=len(seasons),
        assets_stored=assets_stored,
        teams_linked=teams_linked,
    )
    return AvatarBackfillResult(
        seasons_processed=len(seasons),
        assets_stored=assets_stored,
        teams_linked=teams_linked,
    )


__all__ = [
    "DEFAULT_ASSETS_ROOT",
    "AvatarBackfillResult",
    "backfill_team_avatars",
    "download_and_store",
]
