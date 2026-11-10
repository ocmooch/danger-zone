"""Resolve an ADP source player to a canonical ``players.player_id`` (read-only).

ADP rows are a low-fidelity, name-keyed overlay (FFC/MFL expose no NFL ids we
store), so they must **not** flow through ``PlayerResolver`` — that mutates
``players`` (stamps ids, overwrites identity fields). This matcher only reads:
it builds a season-aware name+position index once and answers "which existing
player is this?" conservatively. An ambiguous or absent match returns ``None``;
the runner stores the row with a null ``player_id`` and counts it unresolved,
rather than risk mis-assigning market data to the wrong player.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.repository.models import Player

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ff_pipeline.crawlers.adp.endpoints import AdpEntry

# NFL roster positions folded onto the fantasy universe, so an FFC "PK" matches a
# stored "K" and a "DST"/"D/ST" matches "DEF". Mirrors the dashboard's fold.
_FANTASY_POSITION: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "FB": "RB",
    "HB": "RB",
    "WR": "WR",
    "TE": "TE",
    "K": "K",
    "PK": "K",
    "DEF": "DEF",
    "DST": "DEF",
    "D/ST": "DEF",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def fold_position(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _FANTASY_POSITION.get(raw.strip().upper())


def normalize_name(name: str | None) -> str | None:
    """Lowercase, de-accent, drop punctuation + suffixes; handle ``Last, First``."""
    if not name:
        return None
    text = name.strip()
    if "," in text:  # MFL "Last, First"
        last, _, first = text.partition(",")
        text = f"{first.strip()} {last.strip()}"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.replace("'", "")  # elide apostrophes (D'Andre → DAndre), don't split
    text = re.sub(r"[^a-zA-Z\s]", " ", text).lower()
    tokens = [t for t in text.split() if t and t not in _SUFFIXES]
    return " ".join(tokens) or None


class AdpPlayerMatcher:
    """Read-only name+position+era matcher over the ``players`` table."""

    def __init__(self, session: Session) -> None:
        rows = session.execute(
            select(
                Player.player_id,
                Player.name_full,
                Player.position,
                Player.sleeper_id,
                Player.rookie_year,
                Player.last_season,
                Player.first_rostered_season,
                Player.last_rostered_season,
            )
        ).all()
        # (name_key, fantasy_pos) -> list of candidate player rows
        self._index: dict[tuple[str, str | None], list[_Candidate]] = {}
        self._by_sleeper: dict[str, int] = {}
        for pid, name, pos, sleeper_id, rookie, last, first_r, last_r in rows:
            if sleeper_id:
                self._by_sleeper[str(sleeper_id)] = int(pid)
            key = normalize_name(name)
            if key is None:
                continue
            self._index.setdefault((key, fold_position(pos)), []).append(
                _Candidate(int(pid), rookie, last, first_r, last_r)
            )

    def match(self, entry: AdpEntry, *, year: int) -> int | None:
        """Best canonical ``player_id`` for ``entry``, or ``None`` if unsure."""
        # Direct id path (Sleeper carries a sleeper_id as its key).
        if entry.source == "sleeper":
            direct = self._by_sleeper.get(entry.source_player_key)
            if direct is not None:
                return direct
        key = normalize_name(entry.name)
        if key is None:
            return None
        pos = fold_position(entry.position)
        candidates = list(self._index.get((key, pos), ()))
        if not candidates and pos is not None:
            # Fall back to name-only when the stored position disagrees with the
            # source's (position labels drift; the name+era is the stronger signal).
            for (name_key, _), bucket in self._index.items():
                if name_key == key:
                    candidates.extend(bucket)
        if not candidates:
            return None
        in_era = [c for c in candidates if c.active_in(year)]
        pool = in_era or candidates
        ids = {c.player_id for c in pool}
        if len(ids) == 1:
            return next(iter(ids))
        # Still ambiguous after the era guard — refuse rather than guess.
        return None


class _Candidate:
    __slots__ = ("first_rostered", "last_rostered", "last_season", "player_id", "rookie_year")

    def __init__(
        self,
        player_id: int,
        rookie_year: int | None,
        last_season: int | None,
        first_rostered: int | None,
        last_rostered: int | None,
    ) -> None:
        self.player_id = player_id
        self.rookie_year = rookie_year
        self.last_season = last_season
        self.first_rostered = first_rostered
        self.last_rostered = last_rostered

    def active_in(self, year: int) -> bool:
        """Whether the player's NFL/league window plausibly contains ``year``.

        Uses the NFL career span (rookie_year..last_season) and the league
        roster span when known; unknown bounds don't exclude (absence ≠ no).
        """
        lo = self.rookie_year if self.rookie_year is not None else self.first_rostered
        hi = self.last_season if self.last_season is not None else self.last_rostered
        if lo is not None and year < lo:
            return False
        return not (hi is not None and year > hi)


__all__ = ["AdpPlayerMatcher", "fold_position", "normalize_name"]
