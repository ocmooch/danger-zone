"""Manual owner identity canonicalization.

NFL.com exposes manager history as display names plus opaque ``userId`` values.
Most of the time one userId is one human, but known account/name splits need a
manual override so ingestion keeps one ``owners`` row per real person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ff_pipeline.repository.models import OwnerIdentityOverride

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_SUPPORTED_KINDS = frozenset({"display_name", "nfl_user_id"})


@dataclass(frozen=True, slots=True)
class CanonicalOwnerIdentity:
    """Resolved identity for one parsed NFL.com owner row."""

    key: str
    display_name: str
    observed_display_name: str
    nfl_user_id: str | None
    was_overridden: bool


def canonicalize_owner_identity(
    session: Session,
    *,
    league_id: str,
    display_name: str,
    nfl_user_id: str | None,
) -> CanonicalOwnerIdentity:
    """Return the canonical owner identity for a parsed display/user pair."""

    overrides = _load_overrides(session, league_id)
    canonical = None
    if nfl_user_id:
        canonical = overrides.get(("nfl_user_id", _norm(nfl_user_id)))
    if canonical is None:
        canonical = overrides.get(("display_name", _norm(display_name)))

    if canonical is not None:
        return CanonicalOwnerIdentity(
            key=f"canonical_display_name:{_norm(canonical)}",
            display_name=canonical,
            observed_display_name=display_name,
            nfl_user_id=nfl_user_id,
            was_overridden=True,
        )

    key = f"nfl_user_id:{nfl_user_id}" if nfl_user_id else f"display_name:{_norm(display_name)}"
    return CanonicalOwnerIdentity(
        key=key,
        display_name=display_name,
        observed_display_name=display_name,
        nfl_user_id=nfl_user_id,
        was_overridden=False,
    )


def seed_owner_identity_override(
    session: Session,
    *,
    league_id: str,
    external_id_kind: str,
    external_id_value: str,
    canonical_display_name: str,
    notes: str | None = None,
) -> OwnerIdentityOverride:
    """Create or update one owner identity override row."""

    if external_id_kind not in _SUPPORTED_KINDS:
        raise ValueError(f"unsupported owner override kind: {external_id_kind!r}")
    existing = session.execute(
        select(OwnerIdentityOverride).where(
            OwnerIdentityOverride.league_id == league_id,
            OwnerIdentityOverride.external_id_kind == external_id_kind,
            OwnerIdentityOverride.external_id_value == external_id_value,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.canonical_display_name = canonical_display_name
        existing.notes = notes
        return existing

    override = OwnerIdentityOverride(
        league_id=league_id,
        external_id_kind=external_id_kind,
        external_id_value=external_id_value,
        canonical_display_name=canonical_display_name,
        notes=notes,
    )
    session.add(override)
    return override


def _load_overrides(session: Session, league_id: str) -> dict[tuple[str, str], str]:
    rows = session.execute(
        select(OwnerIdentityOverride).where(OwnerIdentityOverride.league_id == league_id)
    ).scalars()
    overrides: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.external_id_kind not in _SUPPORTED_KINDS:
            continue
        overrides[(row.external_id_kind, _norm(row.external_id_value))] = row.canonical_display_name
    return overrides


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


__all__ = [
    "CanonicalOwnerIdentity",
    "canonicalize_owner_identity",
    "seed_owner_identity_override",
]
