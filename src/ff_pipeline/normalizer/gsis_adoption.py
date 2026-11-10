"""Give Sleeper-linked rookies their real nflverse gsis id (S6).

Sleeper's player directory has no ``gsis_id`` for most rookies, so the
league-rostered row for a rookie (created before the draft, carrying the
``sleeper_id`` and a placeholder id) and the row nflverse creates once the
player records a stat (carrying the real ``00-…`` gsis id and the stats) are
two rows for one person. The reconstruction then has no stats for the
rostered row.

*Adoption* moves the gsis id onto the rostered row and re-homes the twin's
stat rows, so every later nflverse ingest lands on the rostered row. A pair
qualifies only when it is unambiguous:

* the rostered row is in the season's ``team_rosters``, has a ``sleeper_id``
  and no real gsis id (blank, or not ``00-…``);
* exactly one other row has the same full name and position, a real gsis id
  and **no** ``sleeper_id``.

Anything else is reported, never guessed. The twin row stays (gsis cleared)
with a ``player_identity_links`` row pointing at the rostered row. Caller
commits.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from ff_pipeline.repository.models import (
    Player,
    PlayerIdentityLink,
    PlayerSeasonPosition,
    PlayerStatsRaw,
    PlayerStatsScored,
    TeamRoster,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LINK_SOURCE = "gsis_adoption"


@dataclass(frozen=True, slots=True)
class Adoption:
    keep_id: int  # the league-rostered (Sleeper-linked) row
    twin_id: int  # the nflverse row holding the real gsis id
    gsis_id: str
    name: str
    position: str | None
    placeholder: str | None  # the keep row's old non-gsis id, if any


@dataclass(slots=True)
class AdoptionPlan:
    adoptions: list[Adoption] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)  # "name (pos): n candidates"
    no_twin: list[str] = field(default_factory=list)  # rostered, no real gsis, no twin yet


def _real(gsis: str | None) -> bool:
    return bool(gsis) and str(gsis).startswith("00-")


def plan_adoptions(session: Session, *, season: int) -> AdoptionPlan:
    rostered = set(
        session.scalars(select(TeamRoster.player_id).where(TeamRoster.season_year == season))
    )
    keep_rows = [
        p
        for p in session.scalars(
            select(Player).where(Player.player_id.in_(rostered), Player.sleeper_id.is_not(None))
        )
        if not _real(p.gsis_id) and p.position != "DEF"
    ]
    by_name: defaultdict[tuple[str, str | None], list[Player]] = defaultdict(list)
    names = {p.name_full for p in keep_rows}
    for q in session.scalars(
        select(Player).where(
            Player.name_full.in_(names),
            Player.gsis_id.like("00-%"),
            Player.sleeper_id.is_(None),
        )
    ):
        by_name[(q.name_full, q.position)].append(q)
    plan = AdoptionPlan()
    for p in sorted(keep_rows, key=lambda r: r.name_full):
        twins = [
            t for t in by_name.get((p.name_full, p.position), []) if t.player_id != p.player_id
        ]
        label = f"{p.name_full} ({p.position})"
        if len(twins) == 1:
            t = twins[0]
            plan.adoptions.append(
                Adoption(
                    keep_id=p.player_id,
                    twin_id=t.player_id,
                    gsis_id=str(t.gsis_id),
                    name=p.name_full,
                    position=p.position,
                    placeholder=p.gsis_id,
                )
            )
        elif twins:
            plan.ambiguous.append(f"{label}: {len(twins)} candidates")
        else:
            plan.no_twin.append(label)
    return plan


def apply_adoptions(session: Session, adoptions: list[Adoption]) -> int:
    """Move each twin's gsis id and stat rows onto the rostered row."""
    for a in adoptions:
        _rehome(session, a.twin_id, a.keep_id)
        session.execute(update(Player).where(Player.player_id == a.twin_id).values(gsis_id=None))
        session.flush()
        session.execute(
            update(Player).where(Player.player_id == a.keep_id).values(gsis_id=a.gsis_id)
        )
        if (
            session.scalars(
                select(PlayerIdentityLink).where(PlayerIdentityLink.member_player_id == a.twin_id)
            ).first()
            is None
        ):
            session.add(
                PlayerIdentityLink(
                    member_player_id=a.twin_id,
                    canonical_player_id=a.keep_id,
                    source=LINK_SOURCE,
                    confidence="exact_name_position",
                    notes=f"gsis {a.gsis_id} adopted; old placeholder id {a.placeholder or '-'}",
                )
            )
    session.flush()
    return len(adoptions)


def _rehome(session: Session, twin: int, keep: int) -> None:
    """Re-point the twin's stat rows to ``keep``; a row ``keep`` already has
    for the same key wins and the twin's copy is dropped."""
    have_raw = set(
        session.execute(
            select(PlayerStatsRaw.season_year, PlayerStatsRaw.week, PlayerStatsRaw.source).where(
                PlayerStatsRaw.player_id == keep
            )
        ).tuples()
    )
    for stat_id, yr, wk, src in session.execute(
        select(
            PlayerStatsRaw.stat_id,
            PlayerStatsRaw.season_year,
            PlayerStatsRaw.week,
            PlayerStatsRaw.source,
        ).where(PlayerStatsRaw.player_id == twin)
    ).tuples():
        if (yr, wk, src) in have_raw:
            session.execute(delete(PlayerStatsScored).where(PlayerStatsScored.stat_id == stat_id))
            session.execute(delete(PlayerStatsRaw).where(PlayerStatsRaw.stat_id == stat_id))
        else:
            session.execute(
                update(PlayerStatsRaw)
                .where(PlayerStatsRaw.stat_id == stat_id)
                .values(player_id=keep)
            )
            session.execute(
                update(PlayerStatsScored)
                .where(PlayerStatsScored.stat_id == stat_id)
                .values(player_id=keep)
            )
    have_pos = set(
        session.scalars(
            select(PlayerSeasonPosition.season_year).where(PlayerSeasonPosition.player_id == keep)
        )
    )
    for yr in list(
        session.scalars(
            select(PlayerSeasonPosition.season_year).where(PlayerSeasonPosition.player_id == twin)
        )
    ):
        where = (PlayerSeasonPosition.player_id == twin, PlayerSeasonPosition.season_year == yr)
        if yr in have_pos:
            session.execute(delete(PlayerSeasonPosition).where(*where))
        else:
            session.execute(update(PlayerSeasonPosition).where(*where).values(player_id=keep))


__all__ = ["Adoption", "AdoptionPlan", "apply_adoptions", "plan_adoptions"]
