"""Content-addressed raw payload archive + the ``source_payloads`` index (S2).

Every platform API response is stored byte for byte **before** anything
parses it, so a season can be re-normalized offline and deterministically.

* **Bytes:** ``<root>/<sha256[:2]>/<sha256>.json.gz``. The hash is of the raw
  response body (not the gzip file). The gzip header has ``mtime=0`` so the
  same body always produces the same file. Writes are atomic (temp file +
  rename), and an existing file is never rewritten.
* **Index:** one ``source_payloads`` row per (``sha256``, ``request_url``).
  An identical re-fetch bumps ``last_fetched_at`` / ``fetch_count``; changed
  bytes add a new row (the next revision).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ff_pipeline.repository.models import SourcePayload

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session


class RawStoreError(RuntimeError):
    """The archive is missing a file or a file does not match its hash."""


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class RawStore:
    """Gzip'd payload files on disk, addressed by the sha256 of the raw body."""

    root: Path

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise RawStoreError(f"not a sha256 hex digest: {sha256!r}")
        return self.root / sha256[:2] / f"{sha256}.json.gz"

    def put(self, body: bytes) -> tuple[str, bool]:
        """Store ``body``; return ``(sha256, written)``. ``written`` is False
        when the file already existed (dedupe)."""
        sha = sha256_hex(body)
        path = self.path_for(sha)
        if path.exists():
            return sha, False
        path.parent.mkdir(parents=True, exist_ok=True)
        data = gzip.compress(body, compresslevel=9, mtime=0)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".gz", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha, True

    def get(self, sha256: str) -> bytes:
        """Return the raw body; verifies the hash."""
        path = self.path_for(sha256)
        if not path.exists():
            raise RawStoreError(f"raw payload missing from archive: {path}")
        body = gzip.decompress(path.read_bytes())
        if sha256_hex(body) != sha256:
            raise RawStoreError(f"raw payload does not match its hash: {path}")
        return body


@dataclass(frozen=True, slots=True)
class PayloadMeta:
    """What ``record_payload`` needs to know about one fetched response."""

    source: str
    endpoint: str
    request_url: str
    platform_league_id: str | None
    season_year: int | None
    week: int | None
    http_status: int


def record_payload(
    session: Session,
    meta: PayloadMeta,
    *,
    sha256: str,
    byte_size: int,
    fetched_at: datetime,
    run_id: str,
) -> bool:
    """Upsert the index row for one fetch. Returns True for a new revision.

    Same (sha256, request_url) ⇒ bump ``last_fetched_at`` + ``fetch_count``.
    """
    existed = (
        session.scalar(
            select(SourcePayload.payload_id).where(
                SourcePayload.sha256 == sha256,
                SourcePayload.request_url == meta.request_url,
            )
        )
        is not None
    )
    stmt = sqlite_insert(SourcePayload).values(
        source=meta.source,
        endpoint=meta.endpoint,
        platform_league_id=meta.platform_league_id,
        season_year=meta.season_year,
        week=meta.week,
        request_url=meta.request_url,
        sha256=sha256,
        byte_size=byte_size,
        http_status=meta.http_status,
        first_fetched_at=fetched_at,
        last_fetched_at=fetched_at,
        fetch_count=1,
        run_id=run_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["sha256", "request_url"],
        set_={
            "last_fetched_at": stmt.excluded.last_fetched_at,
            "fetch_count": SourcePayload.fetch_count + 1,
        },
    )
    session.execute(stmt)
    return not existed


def newest_payload(
    session: Session, request_url: str, *, as_of: datetime | None = None
) -> SourcePayload | None:
    """The current revision for a request, or the one current at ``as_of``.

    Without ``as_of`` this is the most recently *seen* revision (so a payload
    that reverted to earlier bytes is reported correctly). With ``as_of`` it
    is the newest revision first seen at or before that moment; a revert
    between two runs is not visible there, because only the first and last
    sighting of each revision are kept.
    """
    q = select(SourcePayload).where(SourcePayload.request_url == request_url)
    if as_of is None:
        q = q.order_by(SourcePayload.last_fetched_at.desc(), SourcePayload.payload_id.desc())
    else:
        q = q.where(SourcePayload.first_fetched_at <= as_of).order_by(
            SourcePayload.first_fetched_at.desc(), SourcePayload.payload_id.desc()
        )
    return session.scalars(q.limit(1)).first()


__all__ = [
    "PayloadMeta",
    "RawStore",
    "RawStoreError",
    "newest_payload",
    "record_payload",
    "sha256_hex",
]
