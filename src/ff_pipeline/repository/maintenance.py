"""Recompute derived/materialized columns on ``players``.

Kept separate from the read-only ``queries`` layer (which must not mutate)
and from ``upsert`` (which writes source rows): this module owns the few
columns whose value is *derived* from other tables rather than ingested from
a source. Today that is the league-relevance span
(``first_rostered_season`` / ``last_rostered_season``), recomputed from
``team_rosters`` after each NFL.com roster sync.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select, update

from ff_pipeline.logging_config import get_logger
from ff_pipeline.repository.models import Player, TeamRoster

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)


def recompute_rostered_spans(session: Session) -> int:
    """Refresh ``players.first/last_rostered_season`` from ``team_rosters``.

    Sets each player's span to the MIN/MAX ``team_rosters.season_year`` they
    appear on; a player with no roster row gets NULL spans (the MIN/MAX over
    an empty set), which is the canonical "never rostered here ⇒ not
    league-relevant" marker. Idempotent and self-healing — a full recompute
    each call also corrects any row left stale by an earlier partial load.
    Caller commits. Returns the number of ``players`` rows updated.
    """
    first_sq = (
        select(func.min(TeamRoster.season_year))
        .where(TeamRoster.player_id == Player.player_id)
        .scalar_subquery()
    )
    last_sq = (
        select(func.max(TeamRoster.season_year))
        .where(TeamRoster.player_id == Player.player_id)
        .scalar_subquery()
    )
    result = session.execute(
        update(Player).values(
            first_rostered_season=first_sq,
            last_rostered_season=last_sq,
        )
    )
    updated = cast("CursorResult[Any]", result).rowcount or 0
    log.info("Recomputed rostered-season spans", players_updated=updated)
    return updated


__all__ = ["recompute_rostered_spans"]
