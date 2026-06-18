"""Read-only integrity checks for NFL.com-owned player identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select

from ff_pipeline.repository.models import (
    Player,
    PlayerStatsScored,
    Season,
    TeamRoster,
    Transaction,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


_FANTASY_POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE", "K", "DEF", "DST"})


def source_identity_mismatches(session: Session) -> list[dict[str, Any]]:
    """Return strong evidence that an NFL.com ID belongs to the wrong player.

    The check deliberately avoids subjective name matching. It reports only
    impossible temporal assignments or unsupported-position draft identities
    with no scored data. That catches legacy fuzzy mis-stamps while leaving
    legitimate aliases, position evolution, and retired-player stashes alone.
    """
    roster_spans = {
        int(player_id): (int(first_year), int(last_year), int(row_count))
        for player_id, first_year, last_year, row_count in session.execute(
            select(
                TeamRoster.player_id,
                func.min(TeamRoster.season_year),
                func.max(TeamRoster.season_year),
                func.count(TeamRoster.roster_id),
            ).group_by(TeamRoster.player_id)
        ).all()
    }
    transaction_spans = {
        int(player_id): (int(first_year), int(last_year), int(row_count), int(draft_count or 0))
        for player_id, first_year, last_year, row_count, draft_count in session.execute(
            select(
                Transaction.player_id,
                func.min(Season.year),
                func.max(Season.year),
                func.count(Transaction.transaction_id),
                func.sum(case((Transaction.transaction_type == "draft", 1), else_=0)),
            )
            .join(Season, Season.season_id == Transaction.season_id)
            .where(Transaction.player_id.is_not(None))
            .group_by(Transaction.player_id)
        ).all()
        if player_id is not None
    }
    scored_ids = {
        int(player_id)
        for player_id in session.execute(select(PlayerStatsScored.player_id).distinct()).scalars()
    }

    players = session.execute(
        select(
            Player.player_id,
            Player.name_full,
            Player.position,
            Player.rookie_year,
            Player.last_season,
            Player.gsis_id,
            Player.nfl_com_player_id,
        ).where(Player.nfl_com_player_id.is_not(None))
    ).all()

    out: list[dict[str, Any]] = []
    for row in players:
        player_id = int(row.player_id)
        roster = roster_spans.get(player_id)
        transactions = transaction_spans.get(player_id)
        if roster is None and transactions is None:
            continue
        observed_first = min(span[0] for span in (roster, transactions) if span is not None)
        observed_last = max(span[1] for span in (roster, transactions) if span is not None)
        draft_count = transactions[3] if transactions is not None else 0

        reason: str | None = None
        if row.rookie_year is not None and observed_first < int(row.rookie_year):
            reason = "observed_before_nfl_debut"
        elif (
            row.last_season is not None
            and observed_last > int(row.last_season) + 1
            and player_id not in scored_ids
        ):
            reason = "observed_after_nfl_career_without_stats"
        elif (
            draft_count > 0
            and row.position is not None
            and str(row.position).upper() not in _FANTASY_POSITIONS
            and player_id not in scored_ids
        ):
            reason = "unsupported_draft_position_without_stats"
        if reason is None:
            continue

        out.append(
            {
                "player_id": player_id,
                "name_full": str(row.name_full),
                "position": row.position,
                "rookie_year": row.rookie_year,
                "last_season": row.last_season,
                "first_observed_season": observed_first,
                "last_observed_season": observed_last,
                "nfl_com_player_id": str(row.nfl_com_player_id),
                "gsis_id": row.gsis_id,
                "roster_row_count": roster[2] if roster is not None else 0,
                "transaction_row_count": transactions[2] if transactions is not None else 0,
                "draft_pick_count": draft_count,
                "reason": reason,
            }
        )
    return sorted(out, key=lambda item: (item["first_observed_season"], item["name_full"]))


__all__ = ["source_identity_mismatches"]
