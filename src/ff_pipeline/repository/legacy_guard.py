"""Application-level legacy guard (``03_LEGACY_PRESERVATION.md`` §6.3).

The SQLite triggers are the real wall; this gives writers a fast, readable
failure *before* they reach it. Loaders call ``assert_writable(session,
season_id)`` before writing league facts for a season.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.repository.models import LegacyGuard, Season

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

AMEND_COMMAND = 'ff-pipeline legacy amend --reason "<why>" -- <command>'


class SealedSeasonError(RuntimeError):
    """Raised when code tries to write league facts into a sealed season."""

    def __init__(self, year: int, seal_id: str | None) -> None:
        self.year = year
        self.seal_id = seal_id
        seal = f" ({seal_id})" if seal_id else ""
        super().__init__(
            f"Season {year} is sealed{seal}; its league data can only change through an "
            f"audited amendment: {AMEND_COMMAND}"
        )


def utc_now_text(now: datetime | None = None) -> str:
    """UTC timestamp in the text form the guard triggers compare against."""
    return (now or datetime.now(tz=UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_unlocked(session: Session, *, now: datetime | None = None) -> bool:
    """True while an amendment's unlock window is open."""
    until = session.execute(
        select(LegacyGuard.unlocked_until).where(LegacyGuard.id == 1)
    ).scalar_one_or_none()
    return isinstance(until, str) and until > utc_now_text(now)


def assert_writable(session: Session, season_id: int) -> None:
    """Raise ``SealedSeasonError`` if ``season_id`` is sealed and no unlock is open."""
    row = session.execute(
        select(Season.year, Season.is_sealed, Season.seal_id).where(Season.season_id == season_id)
    ).first()
    if row is None or not row.is_sealed:
        return
    if is_unlocked(session):
        return
    raise SealedSeasonError(row.year, row.seal_id)


__all__ = ["AMEND_COMMAND", "SealedSeasonError", "assert_writable", "is_unlocked", "utc_now_text"]
