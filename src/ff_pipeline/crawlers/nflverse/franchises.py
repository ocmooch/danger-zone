"""Resolve a rostered team-DEF player to its nflverse team abbreviation.

A DEF "player" in our DB is one NFL franchise (e.g. *San Francisco 49ers*
→ ``nfl_team = "SF"``). To attach team-defense stats we have to match that
franchise to the ``team`` abbreviation nflverse used **for the season in
question**, which is not always today's abbreviation:

* Several DEF rows have a blank ``nfl_team`` and only a partial name
  (``Cowboys``, ``Jets``, ``Panthers``, ``Raiders``); we recover the
  abbreviation from the nickname.
* Franchises that relocated carry their *current* code in our DB but
  nflverse keys historical rows under the old code:
  Raiders ``OAK`` → ``LV`` (2020), Chargers ``SD`` → ``LAC`` (2017),
  Rams ``STL`` → ``LA`` (2016).

:func:`resolve_def_team_abbrev` returns the season-correct abbreviation so
the rollup keyed on nflverse codes lines up with the DEF player_id.
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


def resolve_def_team_abbrev(player: Player, season_year: int) -> str | None:
    """Return the nflverse team abbreviation for ``player`` in ``season_year``.

    Prefers the player's stored ``nfl_team``; falls back to the nickname in
    ``name_full``. Applies the relocation table so the abbreviation matches
    what nflverse used that season. Returns ``None`` when neither source
    yields a recognizable franchise (logged once per unresolved player).
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

    reloc = _RELOCATIONS.get(base)
    if reloc is not None:
        cutover_year, old_abbrev = reloc
        if season_year < cutover_year:
            return old_abbrev
    return base


def _current_abbrev(player: Player) -> str | None:
    stored = (player.nfl_team or "").strip().upper()
    if stored:
        return stored
    name = (player.name_full or "").strip().lower()
    if not name:
        return None
    # Match on the last whitespace-delimited token (the nickname), e.g.
    # "Cowboys" or "New York Giants" -> "giants".
    last_word = name.split()[-1]
    return _NICKNAME_TO_ABBREV.get(last_word)


__all__ = ["resolve_def_team_abbrev"]
