"""Sleeper league / user / team avatars → ``assets`` (S7-D8).

Run by ``sleeper-league capture`` (the only network step). URLs come from the
archived league and users (``normalizer.sleeper_league.avatar_urls``); an URL
already stored is skipped without a request, and identical images dedupe by
sha256 (``crawlers.nfl_com.media.download_and_store``). The load links them to
``teams``. Images live under ``ASSETS_DIR`` (gitignored), never in the repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from ff_pipeline.crawlers.nfl_com.media import download_and_store
from ff_pipeline.repository.models import Asset

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session


class HttpBytesFetcher:
    """Minimal ``get_bytes`` fetcher for public CDN images."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def get_html(self, url: str) -> str:  # protocol completeness; unused
        return self._client.get(url).text

    def get_bytes(self, url: str) -> tuple[bytes, str | None]:
        r = self._client.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type")

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True, slots=True)
class AvatarResult:
    already: int
    stored: int
    failed: int
    aliased: int = 0  # bytes already stored under another URL: that team is not linked


def download_avatars(
    session: Session,
    fetcher: HttpBytesFetcher,
    urls: list[tuple[str, str]],
    *,
    league_id: str,
    assets_root: Path,
) -> AvatarResult:
    known = set(
        session.scalars(select(Asset.source_url).where(Asset.source_url.in_([u for _k, u in urls])))
    )
    already = stored = failed = aliased = 0
    for kind, url in dict.fromkeys(urls):
        if url in known:
            already += 1
            continue
        aid = download_and_store(
            session,
            fetcher,
            league_id=league_id,
            kind=kind,
            source_url=url,
            assets_root=assets_root,
            fetch_errors=(httpx.HTTPError,),
        )
        if aid is None:
            failed += 1
        elif (asset := session.get(Asset, aid)) is not None and asset.source_url != url:
            aliased += 1
        else:
            stored += 1
            known.add(url)
    return AvatarResult(already=already, stored=stored, failed=failed, aliased=aliased)


__all__ = ["AvatarResult", "HttpBytesFetcher", "download_avatars"]
