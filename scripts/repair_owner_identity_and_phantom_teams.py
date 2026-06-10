"""Repair owner-identity splits and merge phantom franchise-duplicate team rows.

Both problems trace to an early year-less backfill that stamped each franchise's
*current* (2025) owner identity, name, and franchise id onto every past season.
That left two durable artifacts in ``fantasy.db``:

1. **Owner identity.** The league has two "Dan"s with *distinct* NFL.com logins:
   DJ (``nfl_user_id=179898``, franchise 5, "The Princess McBride", 2010-present)
   and Cheese (``nfl_user_id=7655244``, franchise 7, renamed yearly, 2014-2024).
   Owner 17 (Cheese) was mis-stamped with DJ's ``179898``; with two owner rows
   sharing one user id, franchise re-pointing put DJ's 2025 team under Cheese.
   This corrects owner 17's user id, renames owner 5 -> ``DJ`` / owner 17 ->
   ``Cheese``, seeds durable identity overrides, and re-owns DJ's 2025 team (5)
   and mike's mis-attributed 2015 "Batesohardithurts" team (83, owner 18 -> 3).

2. **Phantom team rows.** 26 ``final_rank IS NULL`` duplicates (``team_id``
   193-222) each carry a franchise's current name back-projected onto a played
   season. 14 also hold the franchise's real week-1 roster snapshot that the
   ranked "survivor" row is missing; this merges that data into the survivor
   (move week-1 rosters, re-point opponent / transaction references, drop the
   duplicate week-1 matchup) and then deletes all 26.

Per-season NFL.com ``/history/{year}/owners`` is the source of truth used to
establish the facts above; mike (167650) did not play 2016-2017 (Adam, then ill
held his franchise), so his empty phantoms for those years are simply removed.

Dry-run by default. ``--apply`` backs up the SQLite file, then commits.
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.models import (
    Matchup,
    Owner,
    Season,
    Team,
    TeamRoster,
    Transaction,
)
from ff_pipeline.repository.owner_identities import seed_owner_identity_override
from ff_pipeline.settings import get_settings

# --- Owner identity facts (from NFL.com /history/{year}/owners) ----------------
DJ_USER_ID = "179898"
CHEESE_USER_ID = "7655244"
DJ_OWNER_ID = 5
CHEESE_OWNER_ID = 17

# (year, team display name, wrong owner id, correct owner id) re-ownerships.
TEAM_REOWNERSHIPS = (
    # mike's 2015 "Batesohardithurts" was stamped onto franchise 12 (Kevin).
    (2015, "Batesohardithurts", 18, 3),
    # DJ's 2025 "The Princess McBride" grabbed by Cheese via the shared user id.
    (2025, "The Princess McBride", CHEESE_OWNER_ID, DJ_OWNER_ID),
)


@dataclass
class MergePlan:
    phantom_id: int
    season_id: int
    owner_id: int
    survivor_id: int | None
    rosters: int
    matchups_self: int
    matchups_opp: int
    txns_team: int
    txns_counterpart: int


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as session:
            print("== Part 1: owner identity ==")
            _repair_owner_identity(session, league_id=args.league_id)
            print("\n== Part 2: phantom franchise-duplicate teams ==")
            plans = _merge_phantom_teams(session)
            print("\n== Validation (post-repair, in transaction) ==")
            ok = _validate(session)

            if not args.apply:
                session.rollback()
                print("\nDry run only; rerun with --apply to commit.")
                return
            if not ok:
                session.rollback()
                raise SystemExit("validation failed; rolled back. Fix inputs and retry.")

            _backup_sqlite(settings.database_url)
            session.commit()
            print(f"\nApplied: split DJ/Cheese, {len(plans)} phantom rows merged/removed.")
    finally:
        engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league-id", default=get_settings().nfl_league_id)
    parser.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    return parser.parse_args()


# --- Part 1 --------------------------------------------------------------------
def _repair_owner_identity(session: Session, *, league_id: str) -> None:
    dj = session.get(Owner, DJ_OWNER_ID)
    cheese = session.get(Owner, CHEESE_OWNER_ID)
    if dj is None or cheese is None:
        raise SystemExit("expected owners 5 (DJ) and 17 (Cheese) not found")

    _set(dj, "display_name", "DJ")
    _set(dj, "nfl_user_id", DJ_USER_ID)
    _set(dj, "is_active", True)
    _set(dj, "left_year", None)

    # Cheese's real login was masked by DJ's; correct it and the name/tenure.
    _set(cheese, "display_name", "Cheese")
    _set(cheese, "nfl_user_id", CHEESE_USER_ID)
    _set(cheese, "is_active", False)
    _set(cheese, "joined_year", 2014)
    _set(cheese, "left_year", 2024)

    # Durable: keep these names through future `reconstruct-owners` runs.
    seed_owner_identity_override(
        session,
        league_id=league_id,
        external_id_kind="nfl_user_id",
        external_id_value=DJ_USER_ID,
        canonical_display_name="DJ",
        notes="DJ and Cheese are two people; this is the long-tenured franchise-5 manager",
    )
    seed_owner_identity_override(
        session,
        league_id=league_id,
        external_id_kind="nfl_user_id",
        external_id_value=CHEESE_USER_ID,
        canonical_display_name="Cheese",
        notes="Cheese is the franchise-7 manager (2014-2024), distinct login from DJ",
    )

    for year, name, wrong_owner, right_owner in TEAM_REOWNERSHIPS:
        team = _team_by_year_name(session, league_id=league_id, year=year, name=name)
        if team is None:
            print(f"  re-own {year} {name!r}: team not found (skipped)")
            continue
        if team.owner_id == right_owner:
            print(f"  re-own {year} {name!r}: already owner {right_owner} (no-op)")
            continue
        if team.owner_id != wrong_owner:
            print(
                f"  re-own {year} {name!r}: unexpected owner {team.owner_id} "
                f"(expected {wrong_owner}); skipped"
            )
            continue
        team.owner_id = right_owner
        print(
            f"  re-own {year} {name!r}: owner {wrong_owner} -> {right_owner} (team {team.team_id})"
        )
    session.flush()


def _team_by_year_name(session: Session, *, league_id: str, year: int, name: str) -> Team | None:
    return session.execute(
        select(Team)
        .join(Season, Season.season_id == Team.season_id)
        .where(Season.league_id == league_id, Season.year == year, Team.team_name == name)
    ).scalar_one_or_none()


# --- Part 2 --------------------------------------------------------------------
def _merge_phantom_teams(session: Session) -> list[MergePlan]:
    plans = [_plan_for(session, team) for team in _phantom_teams(session)]
    for plan in plans:
        _print_plan(plan)
    for plan in plans:
        _apply_plan(session, plan)
    session.flush()
    return plans


def _phantom_teams(session: Session) -> list[Team]:
    """Franchise-duplicate rows: NULL final_rank, in a played season, <=1 matchup."""
    played = select(Matchup.season_id).distinct().scalar_subquery()
    candidates = session.execute(
        select(Team).where(Team.final_rank.is_(None), Team.season_id.in_(played))
    ).scalars()
    out: list[Team] = []
    for team in candidates:
        games = session.scalar(
            select(func.count()).select_from(Matchup).where(Matchup.team_id == team.team_id)
        )
        if (games or 0) <= 1:
            out.append(team)
    return sorted(out, key=lambda t: (t.season_id, t.team_id))


def _plan_for(session: Session, phantom: Team) -> MergePlan:
    survivors = (
        session.execute(
            select(Team.team_id).where(
                Team.season_id == phantom.season_id,
                Team.owner_id == phantom.owner_id,
                Team.final_rank.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    if len(survivors) > 1:
        raise SystemExit(
            f"phantom {phantom.team_id}: ambiguous survivors {survivors} "
            f"(season {phantom.season_id}, owner {phantom.owner_id})"
        )
    survivor_id = survivors[0] if survivors else None
    return MergePlan(
        phantom_id=phantom.team_id,
        season_id=phantom.season_id,
        owner_id=phantom.owner_id,
        survivor_id=survivor_id,
        rosters=_count(session, TeamRoster, TeamRoster.team_id == phantom.team_id),
        matchups_self=_count(session, Matchup, Matchup.team_id == phantom.team_id),
        matchups_opp=_count(session, Matchup, Matchup.opponent_team_id == phantom.team_id),
        txns_team=_count(session, Transaction, Transaction.team_id == phantom.team_id),
        txns_counterpart=_count(
            session, Transaction, Transaction.counterpart_team_id == phantom.team_id
        ),
    )


def _apply_plan(session: Session, plan: MergePlan) -> None:
    phantom = plan.phantom_id
    survivor = plan.survivor_id
    children = (
        plan.rosters
        + plan.matchups_self
        + plan.matchups_opp
        + plan.txns_team
        + plan.txns_counterpart
    )
    if survivor is None:
        if children:
            raise SystemExit(
                f"phantom {phantom}: no survivor but {children} child rows; refusing to delete"
            )
        session.delete(session.get(Team, phantom))
        return

    # Week-1 roster snapshot the ranked survivor lacks -> move it across.
    # team_rosters is UNIQUE(season_year, week, player_id); move only if the
    # survivor's franchise doesn't already hold that (week, player).
    for roster in session.execute(
        select(TeamRoster).where(TeamRoster.team_id == phantom)
    ).scalars():
        clash = session.execute(
            select(TeamRoster).where(
                TeamRoster.season_year == roster.season_year,
                TeamRoster.week == roster.week,
                TeamRoster.player_id == roster.player_id,
                TeamRoster.team_id != phantom,
            )
        ).first()
        if clash is not None:
            session.delete(roster)
        else:
            roster.team_id = survivor

    # The phantom's own matchup duplicates the survivor's same-week matchup
    # (verified identical opponent + scores); drop it.
    for matchup in session.execute(select(Matchup).where(Matchup.team_id == phantom)).scalars():
        twin = session.execute(
            select(Matchup).where(Matchup.team_id == survivor, Matchup.week == matchup.week)
        ).first()
        if twin is None:
            matchup.team_id = survivor  # survivor somehow lacks the week; keep the data
        else:
            session.delete(matchup)

    # Opponent mirror rows point at the phantom; re-point to the survivor.
    for matchup in session.execute(
        select(Matchup).where(Matchup.opponent_team_id == phantom)
    ).scalars():
        matchup.opponent_team_id = None if matchup.team_id == survivor else survivor

    # Transactions have no uniqueness constraint; consolidate onto the survivor.
    for txn in session.execute(select(Transaction).where(Transaction.team_id == phantom)).scalars():
        txn.team_id = survivor
    for txn in session.execute(
        select(Transaction).where(Transaction.counterpart_team_id == phantom)
    ).scalars():
        txn.counterpart_team_id = None if txn.team_id == survivor else survivor

    session.flush()
    session.delete(session.get(Team, phantom))


def _print_plan(plan: MergePlan) -> None:
    target = f"-> survivor {plan.survivor_id}" if plan.survivor_id else "(orphan: delete)"
    print(
        f"  phantom {plan.phantom_id} season {plan.season_id} owner {plan.owner_id} {target}: "
        f"rosters={plan.rosters} matchups={plan.matchups_self}/opp={plan.matchups_opp} "
        f"txns={plan.txns_team}/cp={plan.txns_counterpart}"
    )


# --- Validation (handoff acceptance criteria) ----------------------------------
def _validate(session: Session) -> bool:
    played = select(Matchup.season_id).distinct().scalar_subquery()
    ok = True

    bad_seasons = session.execute(
        select(Season.year, func.count(Team.team_id), func.count(Team.final_rank))
        .join(Team, Team.season_id == Season.season_id)
        .where(Team.season_id.in_(played))
        .group_by(Season.year)
        .having((func.count(Team.final_rank) != 12) | (func.count(Team.team_id) != 12))
    ).all()
    ok &= _check("every played season has exactly 12 ranked teams", not bad_seasons, bad_seasons)

    phantoms = session.execute(
        select(func.count())
        .select_from(Team)
        .where(Team.final_rank.is_(None), Team.season_id.in_(played))
    ).scalar_one()
    ok &= _check("no NULL-final_rank rows in played seasons", phantoms == 0, phantoms)

    managed = session.execute(
        select(func.count()).select_from(
            select(Team.owner_id, Team.season_id)
            .where(Team.season_id.in_(played))
            .distinct()
            .subquery()
        )
    ).scalar_one()
    ok &= _check("192 distinct managed (owner, season) pairs", managed == 192, managed)

    dans = session.execute(
        select(func.count()).select_from(Owner).where(func.lower(Owner.display_name) == "dan")
    ).scalar_one()
    names = {
        o.owner_id: o.display_name
        for o in session.execute(
            select(Owner).where(Owner.owner_id.in_([DJ_OWNER_ID, CHEESE_OWNER_ID]))
        ).scalars()
    }
    dj_2025 = _team_by_year_name(
        session, league_id=get_settings().nfl_league_id, year=2025, name="The Princess McBride"
    )
    identity_ok = (
        dans == 0
        and names.get(DJ_OWNER_ID) == "DJ"
        and names.get(CHEESE_OWNER_ID) == "Cheese"
        and dj_2025 is not None
        and dj_2025.owner_id == DJ_OWNER_ID
    )
    ok &= _check(
        "DJ/Cheese distinct, no 'Dan', owner 5 owns 2025 Princess McBride",
        identity_ok,
        f"dans={dans} names={names} 2025_owner={getattr(dj_2025, 'owner_id', None)}",
    )
    return ok


def _check(label: str, passed: bool, detail: object) -> bool:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}" + ("" if passed else f"  -> {detail}"))
    return passed


# --- helpers -------------------------------------------------------------------
def _set(obj: object, attr: str, value: object) -> None:
    if getattr(obj, attr) != value:
        setattr(obj, attr, value)


def _count(session: Session, model: type, where: object) -> int:
    return session.scalar(select(func.count()).select_from(model).where(where)) or 0


def _backup_sqlite(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    db_path = Path(database_url[len(prefix) :]).resolve()
    if not db_path.exists():
        return
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-pre-identity-phantom-repair-{stamp}{db_path.suffix}"
    shutil.copy2(db_path, dest)
    print(f"  backup: {dest}")


if __name__ == "__main__":
    main()
