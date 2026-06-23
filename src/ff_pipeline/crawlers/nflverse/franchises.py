"""Resolve a rostered team-DEF player to its nflverse team abbreviation.

A DEF "player" in our DB is one NFL franchise (e.g. *San Francisco 49ers*
→ ``nfl_team = "SF"``). To attach team-defense stats we have to match that
franchise to the ``team`` abbreviation nflverse uses for it. nflverse
normalizes every relocated franchise to its **current** code across all of
history — a 2015 Rams row is ``LA`` (never ``STL``), a 2016 Chargers row is
``LAC`` (never ``SD``), a 2019 Raiders row is ``LV`` (never ``OAK``) —
verified directly against ``load_team_stats``. So matching is purely a
current-code lookup; the season does not change which abbreviation to match.

Two wrinkles remain:

* Several DEF rows have a blank ``nfl_team`` and only a partial name
  (``Cowboys``, ``Jets``, ``Panthers``, ``Raiders``); we recover the
  abbreviation from the nickname.
* Our ``players`` table stores the Rams under ``LAR`` while nflverse uses
  ``LA``; that single stored-code alias is folded before matching.

:func:`resolve_def_team_abbrev` returns that current code so the rollup
keyed on nflverse codes lines up with the DEF player_id. (The reverse
mapping — current code → the abbreviation a season *displayed* under, e.g.
``STL`` for 2015 — lives in :func:`historical_team_code`, for presentation
only; it must never be used to key ingest.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ff_pipeline.logging_config import get_logger

if TYPE_CHECKING:
    from ff_pipeline.repository.models import Player

log = get_logger(__name__)

# Team nickname (lower-cased, last word of the display name) → the most
# recent nflverse abbreviation. Covers all 32 franchises so a future DEF
# roster addition resolves without a code change.
_NICKNAME_TO_ABBREV: dict[str, str] = {
    "cardinals": "ARI",
    "falcons": "ATL",
    "ravens": "BAL",
    "bills": "BUF",
    "panthers": "CAR",
    "bears": "CHI",
    "bengals": "CIN",
    "browns": "CLE",
    "cowboys": "DAL",
    "broncos": "DEN",
    "lions": "DET",
    "packers": "GB",
    "texans": "HOU",
    "colts": "IND",
    "jaguars": "JAX",
    "chiefs": "KC",
    "raiders": "LV",
    "chargers": "LAC",
    "rams": "LA",
    "dolphins": "MIA",
    "vikings": "MIN",
    "patriots": "NE",
    "saints": "NO",
    "giants": "NYG",
    "jets": "NYJ",
    "eagles": "PHI",
    "steelers": "PIT",
    "seahawks": "SEA",
    "49ers": "SF",
    "buccaneers": "TB",
    "titans": "TEN",
    "commanders": "WAS",
    "redskins": "WAS",
    "football": "WAS",  # "Washington Football Team"
}

# Current abbreviation → (first season the new code applies, old code).
# A season strictly *before* the cutover uses the old code.
_RELOCATIONS: dict[str, tuple[int, str]] = {
    "LV": (2020, "OAK"),
    "LAC": (2017, "SD"),
    "LA": (2016, "STL"),
}

# Our ``players`` table stores a franchise under a code nflverse never uses in
# its per-week ``team`` / ``team_stats`` columns. Fold the stored code onto the
# nflverse one before matching. The Rams are the only such case: we carry
# "LAR" but nflverse codes them "LA" in *every* season, so without this fold
# the Rams DEF never matches and its team-defense stats are dropped.
_STORED_ABBREV_ALIASES: dict[str, str] = {
    "LAR": "LA",
}


def historical_team_code(current_code: str, season_year: int) -> str:
    """Render a current franchise code as the one used in ``season_year``.

    nflverse normalizes relocated franchises to their *current* code across all
    of history (a 2015 Raider is coded ``LV``, not ``OAK``), so the team stored
    on a stat row is the current code regardless of season. This reverses that
    for display: a season strictly before a franchise's relocation cutover gets
    the old code (``LV``→``OAK`` pre-2020, ``LAC``→``SD`` pre-2017, ``LA``→``STL``
    pre-2016). Non-relocated codes pass through unchanged.
    """
    reloc = _RELOCATIONS.get(current_code.strip().upper())
    if reloc is not None:
        cutover_year, old_abbrev = reloc
        if season_year < cutover_year:
            return old_abbrev
    return current_code


def resolve_def_team_abbrev(player: Player, _season_year: int) -> str | None:
    """Return the nflverse team abbreviation for ``player``.

    Prefers the player's stored ``nfl_team`` (folding the ``LAR``→``LA``
    storage alias); falls back to the nickname in ``name_full``. Returns
    ``None`` when neither source yields a recognizable franchise (logged once
    per unresolved player).

    The season is accepted (for the year-indexed call site) but does **not**
    change the result: nflverse keys every season of a relocated franchise
    under its current code, so the index must too. Back-mapping to the pre-move
    code
    here (the historical bug) keyed pre-relocation DST stats under ``STL`` /
    ``SD`` / ``OAK`` while nflverse delivered ``LA`` / ``LAC`` / ``LV``,
    silently dropping every relocated DST's pre-move seasons.
    """

    base = _current_abbrev(player)
    if base is None:
        log.warning(
            "Could not resolve DEF player to an NFL team",
            player_id=getattr(player, "player_id", None),
            name_full=getattr(player, "name_full", None),
            nfl_team=getattr(player, "nfl_team", None),
        )
        return None
    return base


def _current_abbrev(player: Player) -> str | None:
    stored = (player.nfl_team or "").strip().upper()
    if stored:
        return _STORED_ABBREV_ALIASES.get(stored, stored)
    name = (player.name_full or "").strip().lower()
    if not name:
        return None
    # Match on the last whitespace-delimited token (the nickname), e.g.
    # "Cowboys" or "New York Giants" -> "giants".
    last_word = name.split()[-1]
    return _NICKNAME_TO_ABBREV.get(last_word)


__all__ = ["historical_team_code", "resolve_def_team_abbrev"]
