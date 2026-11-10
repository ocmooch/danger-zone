"""Sleeper league payloads → canonical DTOs (S5). Pure: no DB access, no writes.

Inputs are the archived payloads of one season (``ArchivedLeague``) plus the
identity maps S4 established (Sleeper user → owner, Sleeper player → player).
Mapping rules: ``docs/sleeper/04_SLEEPER_FIELD_MAPPING.md`` §2-§6 and
``docs/plans/S5-canonical-loader.md`` §2 (decisions S5-D2..D7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ff_pipeline.canonical.dto import (
    MatchupDTO,
    RosterRowDTO,
    SeasonBundle,
    SeasonDTO,
    TeamDTO,
    TxLegDTO,
)
from ff_pipeline.normalizer.slots import reserve_slot, starter_slots

if TYPE_CHECKING:
    from ff_pipeline.crawlers.sleeper_league.archive_view import ArchivedLeague

STATUS = {
    "pre_draft": "pre_draft",
    "drafting": "pre_draft",
    "in_season": "in_progress",
    "complete": "completed",
}
ADD_TYPE = {
    "free_agent": "free_agent_add",
    "waiver": "waiver_add",
    "commissioner": "free_agent_add",
}
LOADED_TX_STATUS = frozenset({"complete", "failed"})


class NormalizeError(ValueError):
    """The payloads cannot be mapped (e.g. a user or player with no identity)."""


@dataclass(frozen=True, slots=True)
class IdentityMaps:
    owner_by_user: dict[str, int]  # Sleeper user_id -> owners.owner_id
    owner_names: dict[int, str]  # owner_id -> canonical display name (team-name fallback)
    owner_abbrev: dict[int, str | None]  # owner_id -> latest legacy team_abbrev
    player_ids: dict[str, int]  # Sleeper player id -> players.player_id
    # week -> roster_id -> reserve player ids, only for weeks an archived rosters
    # revision covers (S5-D3); other weeks get the bench_or_reserve flag.
    reserve_by_week: dict[int, dict[int, set[str]]] = field(default_factory=dict)


def team_key(roster_id: Any) -> str:
    return f"sleeper:roster:{int(roster_id)}"


def normalize(data: ArchivedLeague, ids: IdentityMaps) -> SeasonBundle:
    league = data.league
    settings = league.get("settings") or {}
    season = _season(data, settings)
    bundle = SeasonBundle(season=season)
    rosters = {int(r["roster_id"]): r for r in data.rosters}
    bundle.teams = _teams(data, rosters, ids)
    bundle.matchups, week_rosters = _matchups(data, season, ids, bundle.warnings)
    bundle.rosters = _draft_rosters(data, ids, bundle.warnings) + week_rosters
    bundle.transactions = _draft_legs(data, ids, bundle.warnings) + _transactions(
        data, ids, bundle.warnings
    )
    return bundle


# ── season / teams ───────────────────────────────────────────────────────────


def _season(data: ArchivedLeague, settings: dict[str, Any]) -> SeasonDTO:
    start = _int(settings.get("start_week")) or 1
    playoff_start = _int(settings.get("playoff_week_start"))
    rounds = {_int(m.get("r")) for m in data.winners_bracket} - {None}
    status = str(data.league.get("status") or "")
    last_scored = _int(settings.get("last_scored_leg")) or 0
    if status == "complete":
        last_scored = max(last_scored, data.weeks)
    return SeasonDTO(
        year=data.season,
        status=STATUS.get(status, "in_progress"),
        regular_season_weeks=(playoff_start - start) if playoff_start else None,
        playoff_weeks=len(rounds) or None,
        platform_league_id=data.league_id,
        platform_draft_id=_str(data.league.get("draft_id")),
        last_finalized_week=last_scored,
    )


def _teams(
    data: ArchivedLeague, rosters: dict[int, dict[str, Any]], ids: IdentityMaps
) -> list[TeamDTO]:
    users = {str(u.get("user_id")): u for u in data.users}
    slot_of_roster = {
        int(rid): int(slot)
        for slot, rid in ((data.draft or {}).get("slot_to_roster_id") or {}).items()
        if rid is not None
    }
    if not slot_of_roster:  # fall back to the round-1 picks: draft_slot -> roster_id
        slot_of_roster = {
            int(p["roster_id"]): int(p["draft_slot"])
            for p in data.draft_picks
            if p.get("round") == 1 and p.get("roster_id") is not None and p.get("draft_slot")
        }
    teams: list[TeamDTO] = []
    used_names: set[str] = set()
    for rid in sorted(rosters):
        r = rosters[rid]
        user_id = _str(r.get("owner_id"))
        if user_id is None or user_id not in ids.owner_by_user:
            raise NormalizeError(
                f"roster {rid}: Sleeper user {user_id} has no owner identity; run `owners map`"
            )
        owner_id = ids.owner_by_user[user_id]
        meta = (users.get(user_id) or {}).get("metadata") or {}
        name = (meta.get("team_name") or "").strip()
        if not name:  # S5-D5: canonical owner name, not the Sleeper username
            name = f"{ids.owner_names.get(owner_id, f'Owner {owner_id}')}'s Team"
        base, n = name, 2
        while name in used_names:  # keep UNIQUE(season_id, team_name) satisfiable
            name, n = f"{base} ({n})", n + 1
        used_names.add(name)
        s = r.get("settings") or {}
        teams.append(
            TeamDTO(
                team_key=team_key(rid),
                owner_id=owner_id,
                team_name=name,
                team_abbrev=ids.owner_abbrev.get(owner_id),
                draft_position=slot_of_roster.get(rid),
                platform_record=(
                    _int(s.get("wins")) or 0,
                    _int(s.get("losses")) or 0,
                    _int(s.get("ties")) or 0,
                    _points(s.get("fpts"), s.get("fpts_decimal")),
                    _points(s.get("fpts_against"), s.get("fpts_against_decimal")),
                ),
            )
        )
    return teams


# ── matchups + weekly rosters ────────────────────────────────────────────────


def _matchups(
    data: ArchivedLeague, season: SeasonDTO, ids: IdentityMaps, warnings: list[str]
) -> tuple[list[MatchupDTO], list[RosterRowDTO]]:
    positions = data.league.get("roster_positions") or []
    slots = starter_slots(positions)
    by_week = data.matchups_by_week
    matchups: list[MatchupDTO] = []
    rows: list[RosterRowDTO] = []
    for week, entries in sorted(by_week.items()):
        finalized = week <= season.last_finalized_week
        pairs: dict[int, list[dict[str, Any]]] = {}
        for m in entries:
            if m.get("matchup_id") is not None:
                pairs.setdefault(int(m["matchup_id"]), []).append(m)
        reserve = ids.reserve_by_week.get(week)
        for m in entries:
            rid = int(m["roster_id"])
            opp = None
            if m.get("matchup_id") is not None:
                others = [o for o in pairs[int(m["matchup_id"])] if int(o["roster_id"]) != rid]
                opp = others[0] if others else None
            score = _score(m)
            opp_score = _score(opp) if opp else None
            extra: dict[str, Any] = {
                "points_official": _float(m.get("points")),
                "finalized": finalized,
                "sleeper_matchup_id": m.get("matchup_id"),
            }
            if m.get("custom_points") is not None:
                extra["custom_points"] = _float(m["custom_points"])
            matchups.append(
                MatchupDTO(
                    week=week,
                    team_key=team_key(rid),
                    opponent_key=team_key(opp["roster_id"]) if opp else None,
                    team_score=score,
                    opponent_score=opp_score,
                    finalized=finalized,
                    platform_matchup_key=(
                        f"sleeper:{data.league_id}:{week}:{m['matchup_id']}"
                        if m.get("matchup_id") is not None
                        else None
                    ),
                    extra_data=extra,
                )
            )
            rows.extend(_week_rows(m, week, rid, slots, finalized, reserve, ids, warnings))
    return matchups, rows


def _week_rows(
    m: dict[str, Any],
    week: int,
    rid: int,
    slots: list[Any],
    finalized: bool,
    reserve: dict[int, set[str]] | None,
    ids: IdentityMaps,
    warnings: list[str],
) -> list[RosterRowDTO]:
    points = m.get("players_points") or {}
    starters = [str(s) for s in (m.get("starters") or [])]
    rows: list[RosterRowDTO] = []
    seen: set[int] = set()

    def add(sid: str, slot: str, starter: bool, platform_slot: str, **extra: Any) -> None:
        pid = ids.player_ids.get(sid)
        if pid is None:
            warnings.append(f"week {week}: Sleeper player {sid} unresolved; row skipped")
            return
        if pid in seen:
            return
        seen.add(pid)
        rows.append(
            RosterRowDTO(
                week=week,
                team_key=team_key(rid),
                player_id=pid,
                roster_slot=slot,
                is_starter=starter,
                was_locked_at_kickoff=finalized,
                extra_data={
                    "snapshot_kind": "sleeper_matchup",
                    "platform_slot": platform_slot,
                    "platform_points": _float(points.get(sid)),
                    **extra,
                },
            )
        )

    for slot, sid in zip(slots, starters, strict=False):
        if sid in ("0", ""):
            continue  # empty starting slot (NFL.com skips them too)
        add(sid, slot.roster_slot, True, slot.platform_slot)
    reserve_ids = reserve.get(rid) if reserve is not None else None
    for sid in (str(p) for p in (m.get("players") or [])):
        if sid in starters:
            continue
        if reserve_ids is not None and sid in reserve_ids:
            r = reserve_slot()
            add(sid, r.roster_slot, False, r.platform_slot)
        elif reserve_ids is not None:
            add(sid, "BN", False, "BN")
        else:  # S5-D3: no roster revision for this week -> honest gap flag
            add(sid, "BN", False, "BN", slot_certainty="bench_or_reserve")
    return rows


# ── draft ────────────────────────────────────────────────────────────────────


def _draft_legs(data: ArchivedLeague, ids: IdentityMaps, warnings: list[str]) -> list[TxLegDTO]:
    draft_id = _str((data.draft or {}).get("draft_id")) or _str(data.league.get("draft_id"))
    start = _ms((data.draft or {}).get("start_time"))
    teams = max(1, len(data.rosters))
    legs: list[TxLegDTO] = []
    for p in sorted(data.draft_picks, key=lambda p: int(p.get("pick_no") or 0)):
        pick_no = int(p.get("pick_no") or 0)
        sid = _str(p.get("player_id"))
        pid = ids.player_ids.get(sid) if sid else None
        if pid is None:
            warnings.append(f"draft pick {pick_no}: Sleeper player {sid} unresolved")
            continue
        rnd = int(p.get("round") or 0)
        in_round = pick_no - (rnd - 1) * teams
        legs.append(
            TxLegDTO(
                platform_transaction_id=f"draft:{draft_id}:{pick_no}",
                transaction_type="draft",
                direction="add",
                team_key=team_key(p["roster_id"]),
                player_id=pid,
                executed_at=(start + timedelta(seconds=pick_no)) if start else None,
                effective_week=0,
                status="complete",
                notes=f"Round {rnd}, Pick {in_round} (overall {pick_no})",
                extra_data={"is_keeper": True} if p.get("is_keeper") else None,
            )
        )
    return legs


def _draft_rosters(
    data: ArchivedLeague, ids: IdentityMaps, warnings: list[str]
) -> list[RosterRowDTO]:
    _ = warnings
    rows: list[RosterRowDTO] = []
    for p in data.draft_picks:
        pid = ids.player_ids.get(str(p.get("player_id")))
        if pid is None:
            continue
        rows.append(
            RosterRowDTO(
                week=0,
                team_key=team_key(p["roster_id"]),
                player_id=pid,
                roster_slot="BN",
                is_starter=False,
                was_locked_at_kickoff=False,
                extra_data={"snapshot_kind": "draft", "pick_no": p.get("pick_no")},
            )
        )
    return rows


# ── transactions ─────────────────────────────────────────────────────────────


def _transactions(data: ArchivedLeague, ids: IdentityMaps, warnings: list[str]) -> list[TxLegDTO]:
    legs: list[TxLegDTO] = []
    for t in data.transactions:
        status = str(t.get("status") or "")
        if status not in LOADED_TX_STATUS:
            continue  # pending claims are not facts yet
        tid = str(t["transaction_id"])
        kind = str(t.get("type") or "")
        when = _ms(t.get("status_updated")) or _ms(t.get("created"))
        week = _int(t.get("leg"))
        adds = {str(k): int(v) for k, v in (t.get("adds") or {}).items()}
        drops = {str(k): int(v) for k, v in (t.get("drops") or {}).items()}
        settings = t.get("settings") or {}

        def pid(sid: str, _tid: str = tid) -> int | None:
            p = ids.player_ids.get(sid)
            if p is None:
                warnings.append(f"transaction {_tid}: Sleeper player {sid} unresolved")
            return p

        if kind == "trade":
            for sid, to_rid in adds.items():
                from_rid = drops.get(sid)
                p = pid(sid)
                if p is None:
                    continue
                legs.append(
                    TxLegDTO(
                        tid,
                        "trade",
                        "in",
                        team_key(to_rid),
                        p,
                        when,
                        week,
                        status,
                        counterpart_key=team_key(from_rid) if from_rid else None,
                    )
                )
                if from_rid is not None:
                    legs.append(
                        TxLegDTO(
                            tid,
                            "trade",
                            "out",
                            team_key(from_rid),
                            p,
                            when,
                            week,
                            status,
                            counterpart_key=team_key(to_rid),
                        )
                    )
            continue
        add_type = ADD_TYPE.get(kind)
        if add_type is None:
            warnings.append(f"transaction {tid}: unknown type {kind!r}; skipped")
            continue
        extra: dict[str, Any] = {}
        if kind == "waiver" and settings.get("waiver_bid") is not None:
            extra["faab_bid"] = int(settings["waiver_bid"])
        if kind == "commissioner":
            extra["by_commissioner"] = True
        if status == "failed":  # S5-D7: the claim, not its drop, which never happened
            notes = ((t.get("metadata") or {}).get("notes") or "").strip() or None
            if notes:
                extra["failure_reason"] = notes
            if drops:
                extra["intended_drops"] = sorted(drops)
            for sid, rid in adds.items():
                p = pid(sid)
                if p is not None:
                    legs.append(
                        TxLegDTO(
                            tid,
                            add_type,
                            "in",
                            team_key(rid),
                            p,
                            when,
                            week,
                            status,
                            notes=notes,
                            extra_data=dict(extra) or None,
                        )
                    )
            continue
        for sid, rid in adds.items():
            p = pid(sid)
            if p is not None:
                legs.append(
                    TxLegDTO(
                        tid,
                        add_type,
                        "in",
                        team_key(rid),
                        p,
                        when,
                        week,
                        status,
                        extra_data=dict(extra) or None,
                    )
                )
        drop_extra = {"by_commissioner": True} if kind == "commissioner" else None
        for sid, rid in drops.items():
            p = pid(sid)
            if p is not None:
                legs.append(
                    TxLegDTO(
                        tid,
                        "drop",
                        "out",
                        team_key(rid),
                        p,
                        when,
                        week,
                        status,
                        extra_data=drop_extra,
                    )
                )
    return legs


# ── helpers ──────────────────────────────────────────────────────────────────


def _score(m: dict[str, Any] | None) -> float | None:
    if m is None:
        return None
    if m.get("custom_points") is not None:
        return _float(m["custom_points"])
    return _float(m.get("points"))


def _points(whole: Any, decimal: Any) -> float:
    return round((_int(whole) or 0) + (_int(decimal) or 0) / 100, 2)


def _ms(v: Any) -> datetime | None:
    n = _int(v)
    return datetime.fromtimestamp(n / 1000, tz=UTC) if n else None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    return None if v in (None, "") else str(v)


__all__ = ["IdentityMaps", "NormalizeError", "normalize", "team_key"]
