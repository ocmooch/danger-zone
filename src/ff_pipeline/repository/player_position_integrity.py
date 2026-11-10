"""Read-only integrity check for season-correct player positions.

``players.position`` is a single current/last-known snapshot. Where it disagrees
with the season-aware ``player_season_positions`` for a season the player was
actually rostered in, a box score built off the static snapshot misrepresents the
player (a 2014 WR shown as a later-career TE). This surfaces those divergences as
a neutral, removable "needs attention" signal — the standing inventory behind the
season-correct-position work — without itself mutating anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.repository.models import (
    Player,
    PlayerSeasonPosition,
    TeamRoster,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Positions that fold onto a single fantasy home, so a snapshot of "RB" and a
# season position of "HB"/"FB" don't read as a divergence.
_POSITION_FOLD = {"HB": "RB", "FB": "RB"}


def _fold(position: str | None) -> str | None:
    if position is None:
        return None
    return _POSITION_FOLD.get(position, position)


def season_position_divergences(session: Session) -> list[dict[str, Any]]:
    """Players whose static ``position`` differs from a rostered season's position.

    For each player rostered in a real (week > 0) season that also has a stored
    ``player_season_positions`` row, compare the folded static snapshot to the
    folded season position. Returns one entry per player, listing every season
    where the two disagree, sorted by how many seasons diverge (most first).
    """
    # Distinct (player, season) the player was actually rostered, joined to the
    # season-correct position and the static snapshot.
    rostered = (
        select(TeamRoster.player_id, TeamRoster.season_year)
        .where(TeamRoster.week > 0)
        .distinct()
        .subquery()
    )
    rows = session.execute(
        select(
            Player.player_id,
            Player.name_full,
            Player.position,
            rostered.c.season_year,
            PlayerSeasonPosition.position.label("season_position"),
        )
        .join(rostered, rostered.c.player_id == Player.player_id)
        .join(
            PlayerSeasonPosition,
            (PlayerSeasonPosition.player_id == rostered.c.player_id)
            & (PlayerSeasonPosition.season_year == rostered.c.season_year),
        )
    ).all()

    by_player: dict[int, dict[str, Any]] = {}
    for row in rows:
        if _fold(row.position) == _fold(row.season_position):
            continue
        entry = by_player.setdefault(
            int(row.player_id),
            {
                "player_id": int(row.player_id),
                "name_full": str(row.name_full),
                "snapshot_position": row.position,
                "divergent_seasons": [],
            },
        )
        entry["divergent_seasons"].append(
            {"season_year": int(row.season_year), "season_position": row.season_position}
        )

    for entry in by_player.values():
        entry["divergent_seasons"].sort(key=lambda s: s["season_year"])
        entry["divergent_season_count"] = len(entry["divergent_seasons"])

    return sorted(
        by_player.values(),
        key=lambda item: (-item["divergent_season_count"], item["name_full"]),
    )


__all__ = ["season_position_divergences"]
