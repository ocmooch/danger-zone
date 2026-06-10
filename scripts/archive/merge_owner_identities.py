"""Merge duplicate owner rows into one canonical manager identity.

Usage:
    uv run python scripts/merge_owner_identities.py --absorbed Adam --canonical adam
    uv run python scripts/merge_owner_identities.py --absorbed Adam --canonical adam --apply

The script is intentionally conservative. It seeds durable owner identity
overrides in both dry-run and apply planning, reports same-season conflicts,
and refuses to repoint rows if the merge would violate ``UNIQUE(season_id,
owner_id)``.
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.models import Owner, Season, Team
from ff_pipeline.repository.owner_identities import seed_owner_identity_override
from ff_pipeline.settings import get_settings


@dataclass(frozen=True, slots=True)
class SameSeasonConflict:
    year: int
    canonical_team_id: int
    canonical_team_name: str | None
    absorbed_team_id: int
    absorbed_team_name: str | None


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    engine = create_app_engine(settings.database_url)
    with Session(engine) as session:
        canonical_rows = _owners_by_name(session, args.league_id, args.canonical)
        absorbed_rows = _owners_by_name(session, args.league_id, args.absorbed)
        if not canonical_rows:
            raise SystemExit(f"canonical owner {args.canonical!r} not found")
        canonical = _choose_canonical(canonical_rows)
        absorbed = [
            owner
            for owner in [*canonical_rows, *absorbed_rows]
            if owner.owner_id != canonical.owner_id
        ]
        if not absorbed:
            _seed_display_overrides(
                session,
                league_id=args.league_id,
                canonical=args.canonical,
                absorbed=args.absorbed,
            )
            if args.apply:
                session.commit()
            print(f"absorbed owner {args.absorbed!r} not found; overrides are ready")
            return

        conflicts = [
            conflict
            for owner in absorbed
            for conflict in _same_season_conflicts(session, canonical.owner_id, owner.owner_id)
        ]
        _print_plan(session, canonical, absorbed, conflicts)
        _seed_display_overrides(
            session,
            league_id=args.league_id,
            canonical=args.canonical,
            absorbed=args.absorbed,
        )
        _seed_user_id_overrides(
            session, [canonical, *absorbed], canonical_display_name=args.canonical
        )

        if not args.apply:
            session.rollback()
            print("dry run only; rerun with --apply to commit")
            return

        _backup_sqlite(settings.database_url)
        for owner in absorbed:
            _merge_owner_rows(session, canonical, owner)
        session.commit()
        print(
            f"merged {len(absorbed)} owner rows ({args.absorbed}) into "
            f"{canonical.owner_id} ({args.canonical})"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league-id", default=get_settings().nfl_league_id)
    parser.add_argument("--canonical", required=True)
    parser.add_argument("--absorbed", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _owners_by_name(session: Session, league_id: str, display_name: str) -> list[Owner]:
    return list(
        session.execute(
            select(Owner).where(
                Owner.league_id == league_id,
                func.lower(Owner.display_name) == display_name.casefold(),
            )
        )
        .scalars()
        .all()
    )


def _choose_canonical(owners: list[Owner]) -> Owner:
    return sorted(
        owners,
        key=lambda owner: (
            owner.joined_year if owner.joined_year is not None else 9999,
            owner.owner_id,
        ),
    )[0]


def _same_season_conflicts(
    session: Session,
    canonical_owner_id: int,
    absorbed_owner_id: int,
) -> list[SameSeasonConflict]:
    canonical_teams = list(
        session.execute(select(Team).where(Team.owner_id == canonical_owner_id)).scalars()
    )
    absorbed_teams = list(
        session.execute(select(Team).where(Team.owner_id == absorbed_owner_id)).scalars()
    )
    canonical_by_season = {team.season_id: team for team in canonical_teams}
    conflicts: list[SameSeasonConflict] = []
    for absorbed_team in absorbed_teams:
        canonical_team = canonical_by_season.get(absorbed_team.season_id)
        if canonical_team is None:
            continue
        season = session.get(Season, absorbed_team.season_id)
        conflicts.append(
            SameSeasonConflict(
                year=season.year if season else absorbed_team.season_id,
                canonical_team_id=canonical_team.team_id,
                canonical_team_name=canonical_team.team_name,
                absorbed_team_id=absorbed_team.team_id,
                absorbed_team_name=absorbed_team.team_name,
            )
        )
    return conflicts


def _print_plan(
    session: Session,
    canonical: Owner,
    absorbed: list[Owner],
    conflicts: list[SameSeasonConflict],
) -> None:
    canonical_count = session.scalar(
        select(func.count()).select_from(Team).where(Team.owner_id == canonical.owner_id)
    )
    print(f"canonical: {canonical.owner_id} {canonical.display_name!r} teams={canonical_count}")
    for owner in absorbed:
        absorbed_count = session.scalar(
            select(func.count()).select_from(Team).where(Team.owner_id == owner.owner_id)
        )
        print(f"absorbed:  {owner.owner_id} {owner.display_name!r} teams={absorbed_count}")
    if conflicts:
        print("same-season overlaps that will be preserved under one owner:")
        for conflict in conflicts:
            print(
                f"  {conflict.year}: canonical team {conflict.canonical_team_id} "
                f"{conflict.canonical_team_name!r}; absorbed team "
                f"{conflict.absorbed_team_id} {conflict.absorbed_team_name!r}"
            )


def _seed_display_overrides(
    session: Session,
    *,
    league_id: str,
    canonical: str,
    absorbed: str,
) -> None:
    note = f"{absorbed} and {canonical} are the same manager"
    for value in {canonical, absorbed}:
        seed_owner_identity_override(
            session,
            league_id=league_id,
            external_id_kind="display_name",
            external_id_value=value,
            canonical_display_name=canonical,
            notes=note,
        )


def _seed_user_id_overrides(
    session: Session,
    owners: list[Owner],
    *,
    canonical_display_name: str,
) -> None:
    for owner in owners:
        if owner.nfl_user_id:
            seed_owner_identity_override(
                session,
                league_id=owner.league_id,
                external_id_kind="nfl_user_id",
                external_id_value=owner.nfl_user_id,
                canonical_display_name=canonical_display_name,
                notes="owner identity merge",
            )


def _merge_owner_rows(session: Session, canonical: Owner, absorbed: Owner) -> None:
    session.execute(
        update(Team)
        .where(Team.owner_id == absorbed.owner_id)
        .values(owner_id=canonical.owner_id)
        .execution_options(synchronize_session=False)
    )
    canonical.aliases = _merge_aliases(canonical.aliases, absorbed.aliases, absorbed.display_name)
    years = [y for y in (canonical.joined_year, absorbed.joined_year) if y is not None]
    if years:
        canonical.joined_year = min(years)
    canonical.is_active = canonical.is_active or absorbed.is_active
    if canonical.is_active:
        canonical.left_year = None
    else:
        left_years = [y for y in (canonical.left_year, absorbed.left_year) if y is not None]
        canonical.left_year = max(left_years) if left_years else None
    session.execute(
        delete(Owner)
        .where(Owner.owner_id == absorbed.owner_id)
        .execution_options(synchronize_session=False)
    )


def _merge_aliases(*values: object) -> list[str] | None:
    aliases: set[str] = set()
    for value in values:
        if isinstance(value, list):
            aliases.update(str(v) for v in value)
        elif isinstance(value, dict):
            aliases.update(str(v) for v in value.get("display_names", []))
        elif isinstance(value, str):
            aliases.add(value)
    return sorted(aliases) or None


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
    shutil.copy2(db_path, backup_dir / f"{db_path.stem}-pre-owner-merge-{stamp}{db_path.suffix}")


if __name__ == "__main__":
    main()
