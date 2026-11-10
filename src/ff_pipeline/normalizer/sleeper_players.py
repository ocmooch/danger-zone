"""Resolve Sleeper player ids referenced by the league to canonical players (S4).

Order (``01`` §7.2):

1. Team defense codes (``"PHI"``, ``"LAR"``): the DEF row whose ``sleeper_id`` is
   the code, else the DEF row of ``canonical_franchise(code)`` (``LAR`` → ``LA``).
2. ``PlayerResolver.try_match`` (override → direct ids incl. ``sleeper_id`` /
   ``gsis_id`` → season-bounded fuzzy name + position), never creating stubs.
3. A match is folded through ``player_identity_links`` to its canonical player.
4. Anything left is *stub-needed*. Stubs are only allowed for ids the league
   actually rostered, drafted or transacted; S5's loader creates them (S4-D4).

S4 uses this as a dry run (``sleeper-league resolve-players``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ff_pipeline.nfl_teams import canonical_franchise
from ff_pipeline.normalizer.player_ids import PlayerIdentity, PlayerResolver
from ff_pipeline.repository.models import Player, PlayerIdentityLink, PlayerIdOverride

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

REASONS = ("rostered", "reserve", "matchup", "transaction", "draft")


@dataclass(frozen=True, slots=True)
class StubCandidate:
    sleeper_id: str
    name: str
    position: str | None
    nfl_team: str | None
    reasons: tuple[str, ...]


@dataclass(slots=True)
class Resolution:
    resolved: dict[str, int] = field(default_factory=dict)  # sleeper id -> player_id
    by_method: dict[str, int] = field(default_factory=dict)  # method -> count
    methods: dict[str, str] = field(default_factory=dict)  # sleeper id -> method
    linked: int = 0  # matches folded through player_identity_links
    stub_needed: list[StubCandidate] = field(default_factory=list)


def league_referenced_ids(
    rosters: Iterable[dict[str, Any]],
    matchups: Iterable[dict[str, Any]],
    transactions: Iterable[dict[str, Any]],
    draft_picks: Iterable[dict[str, Any]],
) -> dict[str, set[str]]:
    """Every Sleeper player id the league referenced, with why."""
    out: dict[str, set[str]] = {}

    def add(pid: Any, why: str) -> None:
        if pid not in (None, "", "0"):
            out.setdefault(str(pid), set()).add(why)

    for r in rosters:
        for pid in r.get("players") or []:
            add(pid, "rostered")
        for pid in r.get("reserve") or []:
            add(pid, "reserve")
    for m in matchups:
        for pid in m.get("players") or []:
            add(pid, "matchup")
    for t in transactions:
        for pid in list((t.get("adds") or {}).keys()) + list((t.get("drops") or {}).keys()):
            add(pid, "transaction")
    for p in draft_picks:
        add(p.get("player_id"), "draft")
    return out


@dataclass(slots=True)
class _DefIndex:
    by_sleeper: dict[str, int] = field(default_factory=dict)
    by_franchise: dict[str, int] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)  # lower-cased name_full


def _def_index(session: Session) -> _DefIndex:
    idx = _DefIndex()
    for p in session.scalars(select(Player).where(Player.position == "DEF")):
        if p.sleeper_id:
            idx.by_sleeper[p.sleeper_id.upper()] = p.player_id
        franchise = canonical_franchise(p.nfl_team)
        if franchise:
            idx.by_franchise.setdefault(franchise, p.player_id)
        if p.name_full:
            idx.by_name.setdefault(p.name_full.strip().lower(), p.player_id)
    return idx


def _resolve_def(code: str, info: dict[str, Any], idx: _DefIndex) -> tuple[int | None, str]:
    """DEF code → player: sleeper_id, then franchise, then the team name.

    The name step covers legacy DEF rows that carry only a nickname
    (``"Jets"``) and no ``nfl_team``; Sleeper's directory gives the nickname
    as the defense's ``last_name`` and the full name as ``first + last``.
    """
    pid = idx.by_sleeper.get(code)
    if pid is not None:
        return pid, "def_sleeper_id"
    franchise = canonical_franchise(code)
    pid = idx.by_franchise.get(franchise) if franchise else None
    if pid is not None:
        return pid, "def_franchise"
    for name in (_name(info), info.get("last_name")):
        if name:
            pid = idx.by_name.get(str(name).strip().lower())
            if pid is not None:
                return pid, "def_name"
    return None, ""


def resolve(
    session: Session,
    referenced: dict[str, set[str]],
    directory: dict[str, dict[str, Any]],
    *,
    season: int,
) -> Resolution:
    res = Resolution()
    links: dict[int, int] = dict(
        session.execute(
            select(PlayerIdentityLink.member_player_id, PlayerIdentityLink.canonical_player_id)
        )
        .tuples()
        .all()  # a Result has .keys(), so dict() would treat it as a mapping
    )
    defs = _def_index(session)
    resolver = PlayerResolver(session)

    for sid in sorted(referenced):
        info = directory.get(sid) or {}
        pid: int | None = None
        method = ""
        if not sid.isdigit():
            pid, method = _resolve_def(sid.upper(), info, defs)
        else:
            identity = PlayerIdentity(
                name_full=_name(info) or sid,
                name_first=info.get("first_name"),
                name_last=info.get("last_name"),
                position=info.get("position"),
                nfl_team=info.get("team"),
                gsis_id=(info.get("gsis_id") or "").strip() or None,
                sleeper_id=sid,
            )
            pid = resolver.try_match(identity, source="sleeper", season=season)
            method = "matched"
        if pid is None:
            res.stub_needed.append(
                StubCandidate(
                    sleeper_id=sid,
                    name=_name(info) or "(not in directory)",
                    position=info.get("position"),
                    nfl_team=info.get("team"),
                    reasons=tuple(r for r in REASONS if r in referenced[sid]),
                )
            )
            continue
        if pid in links:
            pid = links[pid]
            res.linked += 1
        res.resolved[sid] = pid
        res.by_method[method] = res.by_method.get(method, 0) + 1
        res.methods[sid] = method
    return res


def _name(info: dict[str, Any]) -> str | None:
    full = info.get("full_name")
    if full:
        return str(full)
    parts = [str(x) for x in (info.get("first_name"), info.get("last_name")) if x]
    return " ".join(parts) or None


class OverrideConflictError(ValueError):
    """The Sleeper id is already pinned to a different player."""


def add_sleeper_override(
    session: Session, sleeper_id: str, player_id: int, *, notes: str | None = None
) -> bool:
    """Pin a Sleeper id to an existing player (``player_id_overrides``, kind ``sleeper_id``).

    For players the fuzzy rules cannot match (a nickname, a position Sleeper
    labels differently). Returns True when a row was added, False when the same
    pin already exists. The caller commits.
    """
    if session.get(Player, player_id) is None:
        raise ValueError(f"player_id {player_id} does not exist")
    existing = session.scalars(
        select(PlayerIdOverride).where(
            PlayerIdOverride.external_id_kind == "sleeper_id",
            PlayerIdOverride.external_id_value == sleeper_id,
        )
    ).first()
    if existing is not None:
        if existing.player_id != player_id:
            raise OverrideConflictError(
                f"Sleeper id {sleeper_id} is already pinned to player {existing.player_id}"
            )
        return False
    session.add(
        PlayerIdOverride(
            external_id_kind="sleeper_id",
            external_id_value=sleeper_id,
            player_id=player_id,
            notes=notes,
        )
    )
    return True


__all__ = [
    "OverrideConflictError",
    "Resolution",
    "StubCandidate",
    "add_sleeper_override",
    "league_referenced_ids",
    "resolve",
]
