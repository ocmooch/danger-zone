"""Platform user → canonical owner crosswalk (D-3, S4).

The crosswalk is a reviewed YAML file (``data/owner_platform_identities.yaml``,
local and gitignored because it holds real names). Nothing is matched by name
automatically: display names already collide across this league's history.

* ``load_crosswalk`` parses the YAML strictly.
* ``validate`` checks it against the archived ``users`` / ``rosters`` payloads
  and the ``owners`` table, and returns every problem found (empty = valid).
* ``apply`` upserts ``owner_platform_identities`` (idempotent).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ff_pipeline.repository.models import Owner, OwnerPlatformIdentity

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

ACCEPTED_CONFIDENCE = frozenset({"high", "confirmed"})
ENTRY_KEYS = frozenset(
    {
        "sleeper_user_id",
        "sleeper_display_name",
        "sleeper_team_name",
        "roster_id",
        "owner_id",
        "confidence",
        "new_owner",
        "owner_display_name",
        "notes",
    }
)


class CrosswalkError(ValueError):
    """The crosswalk file is malformed."""


@dataclass(frozen=True, slots=True)
class CrosswalkEntry:
    user_id: str
    display_name: str | None
    roster_id: int | None
    owner_id: int | None
    confidence: str
    new_owner: bool = False
    owner_display_name: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class Crosswalk:
    platform: str
    league_id: str
    entries: list[CrosswalkEntry] = field(default_factory=list)


def load_crosswalk(path: Path) -> Crosswalk:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("identities"), list):
        raise CrosswalkError(f"{path}: expected keys `platform`, `league_id`, `identities: [...]`")
    entries: list[CrosswalkEntry] = []
    for i, raw in enumerate(data["identities"], start=1):
        if not isinstance(raw, dict):
            raise CrosswalkError(f"{path}: identity #{i} is not a mapping")
        unknown = set(raw) - ENTRY_KEYS
        if unknown:
            raise CrosswalkError(f"{path}: identity #{i} has unknown keys {sorted(unknown)}")
        if raw.get("sleeper_user_id") in (None, ""):
            raise CrosswalkError(f"{path}: identity #{i} has no sleeper_user_id")
        entries.append(
            CrosswalkEntry(
                user_id=str(raw["sleeper_user_id"]),
                display_name=_opt_str(raw.get("sleeper_display_name")),
                roster_id=_opt_int(raw.get("roster_id")),
                owner_id=_opt_int(raw.get("owner_id")),
                confidence=str(raw.get("confidence") or ""),
                new_owner=bool(raw.get("new_owner", False)),
                owner_display_name=_opt_str(raw.get("owner_display_name")),
                notes=_opt_str(raw.get("notes")),
            )
        )
    return Crosswalk(
        platform=str(data.get("platform") or "sleeper"),
        league_id=str(data.get("league_id") or ""),
        entries=entries,
    )


def validate(
    cw: Crosswalk,
    users: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    known_owner_ids: set[int],
) -> list[str]:
    """Every problem with the crosswalk for one league season (empty = valid)."""
    problems: list[str] = []
    by_user = {e.user_id: e for e in cw.entries}

    for user_id, n in Counter(e.user_id for e in cw.entries).items():
        if n > 1:
            problems.append(f"Sleeper user {user_id} appears {n} times")
    owners = [e.owner_id for e in cw.entries if e.owner_id is not None]
    for owner_id, n in Counter(owners).items():
        if n > 1:
            problems.append(f"owner_id {owner_id} is mapped to {n} Sleeper users")

    for e in cw.entries:
        if e.confidence not in ACCEPTED_CONFIDENCE:
            problems.append(
                f"Sleeper user {e.user_id}: confidence {e.confidence!r} is not confirmed "
                f"(accepted: {sorted(ACCEPTED_CONFIDENCE)})"
            )
        if e.new_owner:
            if e.owner_id is not None:
                problems.append(f"Sleeper user {e.user_id}: new_owner with an owner_id")
            if not e.owner_display_name:
                problems.append(f"Sleeper user {e.user_id}: new_owner needs owner_display_name")
        elif e.owner_id is None:
            problems.append(f"Sleeper user {e.user_id}: owner_id is empty")
        elif e.owner_id not in known_owner_ids:
            problems.append(f"Sleeper user {e.user_id}: owner_id {e.owner_id} is not in owners")

    for u in users:
        uid = str(u.get("user_id"))
        if uid not in by_user:
            problems.append(_stanza(uid, u, rosters))

    roster_owner = {r.get("roster_id"): _opt_str(r.get("owner_id")) for r in rosters}
    for e in cw.entries:
        if e.roster_id is not None and e.roster_id in roster_owner:
            actual = roster_owner[e.roster_id]
            if actual != e.user_id:
                problems.append(
                    f"Sleeper user {e.user_id}: roster_id {e.roster_id} is owned by {actual} "
                    "in the archived rosters"
                )
    for r in rosters:
        for co in r.get("co_owners") or []:
            if str(co) not in by_user:
                problems.append(
                    f"co-owner {co} of roster {r.get('roster_id')} is not in the crosswalk"
                )
    return problems


def _stanza(user_id: str, user: dict[str, Any], rosters: list[dict[str, Any]]) -> str:
    roster = next((r.get("roster_id") for r in rosters if str(r.get("owner_id")) == user_id), None)
    team = ((user.get("metadata") or {}).get("team_name") or "").replace('"', "'")
    return (
        f"Sleeper user {user_id} is not mapped. Add to the crosswalk:\n"
        f"  - sleeper_user_id: '{user_id}'\n"
        f'    sleeper_display_name: "{user.get("display_name") or ""}"\n'
        f'    sleeper_team_name: "{team}"\n'
        f"    roster_id: {roster if roster is not None else 'null'}\n"
        "    owner_id: <existing owners.owner_id>   # or new_owner: true + owner_display_name\n"
        "    confidence: confirmed"
    )


@dataclass(slots=True)
class ApplyResult:
    inserted: int = 0
    updated: int = 0
    new_owners: int = 0


def apply(session: Session, cw: Crosswalk, *, season: int, league_id: str) -> ApplyResult:
    """Upsert ``owner_platform_identities`` for a validated crosswalk (caller commits)."""
    result = ApplyResult()
    for e in cw.entries:
        owner_id = e.owner_id
        if e.new_owner:
            owner = Owner(league_id=league_id, display_name=e.owner_display_name, is_active=True)
            session.add(owner)
            session.flush()
            owner_id = owner.owner_id
            result.new_owners += 1
        assert owner_id is not None
        existing = session.scalars(
            select(OwnerPlatformIdentity).where(
                OwnerPlatformIdentity.platform == cw.platform,
                OwnerPlatformIdentity.external_user_id == e.user_id,
            )
        ).first()
        first = min(season, existing.first_season or season) if existing else season
        last = max(season, existing.last_season or season) if existing else season
        stmt = sqlite_insert(OwnerPlatformIdentity).values(
            owner_id=owner_id,
            platform=cw.platform,
            external_user_id=e.user_id,
            external_display_name=e.display_name,
            first_season=first,
            last_season=last,
            notes=e.notes,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["platform", "external_user_id"],
            set_={
                "owner_id": stmt.excluded.owner_id,
                "external_display_name": stmt.excluded.external_display_name,
                "first_season": stmt.excluded.first_season,
                "last_season": stmt.excluded.last_season,
                "notes": stmt.excluded.notes,
            },
        )
        session.execute(stmt)
        if existing is None:
            result.inserted += 1
        else:
            result.updated += 1
    return result


def _opt_str(v: Any) -> str | None:
    return None if v is None or str(v).strip() == "" else str(v)


def _opt_int(v: Any) -> int | None:
    if v is None or str(v).strip() in {"", "?", "null"}:
        return None
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise CrosswalkError(f"not an integer: {v!r}") from exc


__all__ = [
    "ACCEPTED_CONFIDENCE",
    "ApplyResult",
    "Crosswalk",
    "CrosswalkEntry",
    "CrosswalkError",
    "apply",
    "load_crosswalk",
    "validate",
]
