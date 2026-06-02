"""Cross-source player identity resolution.

The normalizer's single job is: given a description of a player from
*any* source — by name, by gsis_id, by sleeper_id, by nfl_com_player_id —
return the canonical internal ``players.player_id``, creating the row if
no match exists and merging in any newly-seen IDs.

Why this exists
---------------

Without the resolver, every crawler has its own ad-hoc
look-up-or-create logic and the three crawlers each accrete their own
duplicate rows. The Sleeper runner today already comments:

    Players Sleeper knows about but nflverse hasn't surfaced yet are
    *skipped*, not stubbed. The full normalizer (M7) is responsible for
    reconciling Sleeper-only players against our identity table — adding
    another stub-creation path here would pollute ``players`` with
    duplicates the normalizer would then have to merge.

This module is that normalizer.

Resolution order (highest priority first)
-----------------------------------------

1. **Override table** — ``player_id_overrides`` pins an external ID to a
   specific internal ``player_id``. Always wins.
2. **Direct ID match** — every populated external ID on the incoming
   identity (``gsis_id``, ``sleeper_id``, ``nfl_com_player_id``,
   ``espn_id``, ``yahoo_id``) is queried in turn against the existing
   ``players`` rows. First hit wins.
3. **Fuzzy name + position** — when no ID matches, fall back to
   ``thefuzz`` against the existing ``name_full`` index, narrowed to
   players with the same position (or NULL position, to handle stubs
   created without a position). A ratio at or above
   :data:`FUZZY_MATCH_THRESHOLD` is accepted.
4. **Create new row** — otherwise insert a fresh player with whatever
   identity fields we have.

Merging
-------

Once a match is found, any *new* (NULL → value) external IDs on the
incoming identity are stamped onto the existing row, and identity fields
(name, position, nfl_team) are overwritten only when the incoming source
has higher precedence than the source that last wrote them, per
:mod:`ff_pipeline.normalizer.conflicts`. We track the "last identity
source" loosely via ``extra_data`` on the resolver call, not as a column
— the resolver is called from runners that already know their own source
name.

The resolver caches lookups for the lifetime of one instance; callers
should construct a fresh one per pipeline run so the cache reflects
in-flight writes from the same run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from thefuzz import fuzz

from ff_pipeline.logging_config import get_logger
from ff_pipeline.normalizer.conflicts import Source, is_higher_precedence
from ff_pipeline.repository.models import Player, PlayerIdOverride

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

log = get_logger(__name__)


#: Minimum ``thefuzz.token_sort_ratio`` (0-100) to accept a fuzzy match.
#: 88 is empirically tight enough to reject most cross-player collisions
#: (e.g. "Mike Williams" disambiguation) while still catching common
#: spelling drift like "Marvin Mims Jr." vs "Marvin Mims" and "DJ Moore"
#: vs "D.J. Moore".
FUZZY_MATCH_THRESHOLD = 88


#: NFL.com assigns every team defense a ``nfl_com_player_id`` in this
#: contiguous range. We use it to recognize a team defense whose scraped
#: position is unknown — a defense scraped while its NFL.com page shows a
#: non-position banner ("Season is Over Add to Watch List") arrives with
#: ``position=None`` because ``_clean_position`` correctly rejects the
#: banner. For these well-known ids the position is unambiguously "DEF",
#: so we stamp it rather than leaving the row NULL. Mirrors the DEF
#: handling in :mod:`ff_pipeline.crawlers.nflverse.runner`.
_NFL_COM_DST_ID_MIN = 100001
_NFL_COM_DST_ID_MAX = 100032


#: External ID columns on ``players`` that the resolver knows about,
#: ordered by precedence. ``gsis_id`` is the strongest because it's the
#: canonical NFL ID nflverse uses; everything else is a join key for one
#: of the crawler sources.
_EXTERNAL_ID_KINDS: tuple[str, ...] = (
    "gsis_id",
    "sleeper_id",
    "nfl_com_player_id",
    "espn_id",
    "yahoo_id",
)


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    """Everything one source observed about a player on one call.

    ``name_full`` is the only field guaranteed by the resolver to be set
    on the returned ``players`` row (when creating fresh); callers should
    populate as many of the other fields as their source emits so later
    runs through the resolver can merge them in.
    """

    name_full: str
    name_first: str | None = None
    name_last: str | None = None
    position: str | None = None
    nfl_team: str | None = None
    gsis_id: str | None = None
    sleeper_id: str | None = None
    nfl_com_player_id: str | None = None
    espn_id: str | None = None
    yahoo_id: str | None = None


@dataclass(slots=True)
class ResolveStats:
    """Aggregate counters from one resolver instance's lifetime.

    Exposed to callers so runners can log "resolver: created N, merged M,
    fuzzy-matched K" alongside their existing summaries. The per-kind
    breakdown lets the Sleeper runner report how many ``sleeper_id``
    values it stamped without conflating that with merged ``espn_id`` /
    ``yahoo_id`` fills from the same pass.
    """

    created: int = 0
    matched_by_override: int = 0
    matched_by_direct_id: int = 0
    matched_by_fuzzy: int = 0
    merged_ids: int = 0
    merged_ids_by_kind: dict[str, int] = field(default_factory=dict)
    merged_fields: int = 0
    fuzzy_rejected_below_threshold: int = 0
    fuzzy_rejected_conflicting_id: int = 0
    last_identity_source_by_player: dict[int, Source] = field(default_factory=dict)


class PlayerResolver:
    """Resolve an incoming :class:`PlayerIdentity` to a ``players.player_id``.

    One instance caches everything it sees so a runner processing
    thousands of identities in a row does at most a handful of SELECTs
    per ID kind, not one per row.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._overrides: dict[tuple[str, str], int] = self._load_overrides()
        # external_id_kind -> {external_value -> player_id}
        self._direct_index: dict[str, dict[str, int]] = {kind: {} for kind in _EXTERNAL_ID_KINDS}
        # Lazy-loaded on first fuzzy lookup; refreshed after creates.
        self._fuzzy_index: list[tuple[int, str, str | None]] | None = None
        self._fuzzy_dirty = True
        self.stats = ResolveStats()
        self._prime_direct_index()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, identity: PlayerIdentity, *, source: Source) -> int:
        """Return the internal ``player_id`` for ``identity``.

        Side effects: creates a new ``players`` row if no match exists,
        merges any newly-seen external IDs onto the matched row, and
        overwrites identity fields when ``source`` outranks the previous
        identity source per :mod:`conflicts`.
        """

        matched = self.try_match(identity, source=source)
        if matched is not None:
            return matched
        return self._create(identity, source=source)

    def resolve_many(
        self,
        identities: list[PlayerIdentity],
        *,
        source: Source,
    ) -> list[int]:
        """Convenience wrapper — resolve a batch, preserving input order."""
        return [self.resolve(i, source=source) for i in identities]

    def try_match(self, identity: PlayerIdentity, *, source: Source) -> int | None:
        """Resolve ``identity`` *without* creating a stub on miss.

        Same precedence chain as :meth:`resolve` (override → direct ID →
        fuzzy), but returns ``None`` if no existing player matches. Used
        by sources like Sleeper that observe far more players than the
        league actually cares about (every NFL player is in Sleeper's
        ``/players/nfl`` feed) — we don't want 11k stub rows just to
        store an ID mapping that may never get joined against.
        """
        # Lookups are listed in priority order. Each entry pairs a label
        # (used for stats bookkeeping) with the bound lookup method —
        # comparing bound-method identity via ``is`` is unreliable since
        # ``self._lookup_x`` creates a fresh bound method on each access.
        lookups: tuple[tuple[str, Callable[[PlayerIdentity], int | None]], ...] = (
            ("override", self._lookup_override),
            ("direct_id", self._lookup_direct),
            ("fuzzy", self._lookup_fuzzy),
        )
        for label, lookup in lookups:
            pid = lookup(identity)
            if pid is not None:
                self._record_match(label, identity, pid, source=source)
                return pid
        return None

    def _record_match(
        self,
        label: str,
        identity: PlayerIdentity,
        player_id: int,
        *,
        source: Source,
    ) -> None:
        if label == "override":
            self.stats.matched_by_override += 1
        elif label == "direct_id":
            self.stats.matched_by_direct_id += 1
        else:
            self.stats.matched_by_fuzzy += 1
        self._merge_into(player_id, identity, source=source)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _lookup_override(self, identity: PlayerIdentity) -> int | None:
        for kind in _EXTERNAL_ID_KINDS:
            value = _id_value(identity, kind)
            if value is None:
                continue
            pid = self._overrides.get((kind, value))
            if pid is not None:
                return pid
        return None

    def _lookup_direct(self, identity: PlayerIdentity) -> int | None:
        matches: list[tuple[str, int]] = []
        for kind in _EXTERNAL_ID_KINDS:
            value = _id_value(identity, kind)
            if value is None:
                continue
            pid = self._direct_index[kind].get(value)
            if pid is not None:
                matches.append((kind, pid))

        if not matches:
            return None

        # If multiple ID kinds match the *same* player, that's the normal
        # happy case. If they match *different* players, prefer the
        # higher-priority kind (kinds are listed in priority order in
        # _EXTERNAL_ID_KINDS) and log the disagreement so an override can
        # be added.
        unique_pids = {pid for _, pid in matches}
        if len(unique_pids) > 1:
            log.warning(
                "Conflicting direct ID matches for player; preferring higher-priority kind",
                identity_name=identity.name_full,
                matches=[{"kind": k, "player_id": p} for k, p in matches],
            )
        return matches[0][1]

    def _lookup_fuzzy(self, identity: PlayerIdentity) -> int | None:
        index = self._get_fuzzy_index()
        if not index:
            return None

        target_position = (identity.position or "").upper() or None

        best_score = 0
        best_pid: int | None = None
        for pid, name_full, position in index:
            if target_position and position and position.upper() != target_position:
                continue
            score = fuzz.token_sort_ratio(identity.name_full, name_full)
            if score > best_score:
                best_score = score
                best_pid = pid

        if best_pid is None or best_score < FUZZY_MATCH_THRESHOLD:
            if best_pid is not None:
                self.stats.fuzzy_rejected_below_threshold += 1
            return None

        # Final safety check: don't accept a fuzzy match if the candidate
        # already has a *different* non-NULL external ID for any kind the
        # incoming identity also has. That would collapse two distinct
        # players (the fuzzy match is name-similarity only and can't
        # adjudicate ID conflicts).
        if self._has_conflicting_id(best_pid, identity):
            self.stats.fuzzy_rejected_conflicting_id += 1
            log.info(
                "Fuzzy match rejected: candidate has conflicting external ID",
                identity_name=identity.name_full,
                candidate_player_id=best_pid,
                score=best_score,
            )
            return None

        log.debug(
            "Fuzzy match accepted",
            identity_name=identity.name_full,
            matched_player_id=best_pid,
            score=best_score,
        )
        return best_pid

    def _has_conflicting_id(self, player_id: int, identity: PlayerIdentity) -> bool:
        player = self._session.get(Player, player_id)
        if player is None:
            return False
        for kind in _EXTERNAL_ID_KINDS:
            incoming = _id_value(identity, kind)
            existing = getattr(player, kind, None)
            if incoming and existing and incoming != existing:
                return True
        return False

    # ------------------------------------------------------------------
    # Merge / create
    # ------------------------------------------------------------------

    def _merge_into(
        self,
        player_id: int,
        identity: PlayerIdentity,
        *,
        source: Source,
    ) -> None:
        """Stamp NULL → value IDs and update identity fields when source ranks higher."""
        player = self._session.get(Player, player_id)
        if player is None:  # pragma: no cover — defensive, FK ensures presence
            return

        # Fill in any blank external IDs we now have a value for.
        ids_merged = 0
        for kind in _EXTERNAL_ID_KINDS:
            incoming = _id_value(identity, kind)
            if incoming is None:
                continue
            existing = getattr(player, kind, None)
            if existing is None:
                setattr(player, kind, incoming)
                self._direct_index[kind][incoming] = player_id
                ids_merged += 1
                self.stats.merged_ids_by_kind[kind] = self.stats.merged_ids_by_kind.get(kind, 0) + 1
            elif existing != incoming:
                # Conflicting non-NULL ID — don't overwrite, log and let
                # an operator add an override if needed.
                log.warning(
                    "Refusing to overwrite existing external ID",
                    player_id=player_id,
                    kind=kind,
                    existing=existing,
                    incoming=incoming,
                    source=source,
                )
        self.stats.merged_ids += ids_merged

        # Identity-field merge — name / position / team. Two rules apply:
        #
        # * Blank → value: always fill in (no precedence concern; we're
        #   completing a partial row).
        # * Value → different value: only overwrite when we *recorded*
        #   that the incumbent value came from a lower-priority source.
        #   An unrecorded incumbent (e.g. an nflverse-created row this
        #   resolver instance hasn't seen yet this run) is treated as
        #   higher precedence than the new source — conservative, since
        #   the alternative is a Sleeper-driven resolve clobbering an
        #   nflverse-curated name.
        incumbent_source = self.stats.last_identity_source_by_player.get(player_id)
        fields_changed = 0
        for attr, new_value in (
            ("name_full", identity.name_full),
            ("name_first", identity.name_first),
            ("name_last", identity.name_last),
            ("position", _effective_position(identity)),
            ("nfl_team", identity.nfl_team),
        ):
            if new_value is None:
                continue
            current = getattr(player, attr, None)
            if current == new_value:
                continue
            if current is None:
                setattr(player, attr, new_value)
                fields_changed += 1
                continue
            # Different non-NULL value — only overwrite with explicit
            # precedence evidence.
            if incumbent_source is not None and is_higher_precedence(
                source, incumbent_source, "identity"
            ):
                setattr(player, attr, new_value)
                fields_changed += 1
        if fields_changed:
            self.stats.merged_fields += fields_changed
            # Player name changed → fuzzy index is stale.
            self._fuzzy_dirty = True
        # Track this source as the most recent identity writer regardless
        # of whether any field changed — so subsequent calls can apply the
        # precedence rules.
        if is_higher_precedence(source, incumbent_source, "identity"):
            self.stats.last_identity_source_by_player[player_id] = source

    def _create(self, identity: PlayerIdentity, *, source: Source) -> int:
        player = Player(
            name_full=identity.name_full,
            name_first=identity.name_first,
            name_last=identity.name_last,
            position=_effective_position(identity),
            nfl_team=identity.nfl_team,
            gsis_id=identity.gsis_id,
            sleeper_id=identity.sleeper_id,
            nfl_com_player_id=identity.nfl_com_player_id,
            espn_id=identity.espn_id,
            yahoo_id=identity.yahoo_id,
            is_active=True,
        )
        self._session.add(player)
        self._session.flush()
        pid = player.player_id
        for kind in _EXTERNAL_ID_KINDS:
            value = _id_value(identity, kind)
            if value:
                self._direct_index[kind][value] = pid
        self._fuzzy_dirty = True
        self.stats.created += 1
        self.stats.last_identity_source_by_player[pid] = source
        return pid

    # ------------------------------------------------------------------
    # Cache priming
    # ------------------------------------------------------------------

    def _load_overrides(self) -> dict[tuple[str, str], int]:
        rows = self._session.execute(
            select(
                PlayerIdOverride.external_id_kind,
                PlayerIdOverride.external_id_value,
                PlayerIdOverride.player_id,
            )
        ).all()
        return {(kind, value): pid for kind, value, pid in rows}

    def _prime_direct_index(self) -> None:
        """Bulk-load the (id_kind → external_value → player_id) maps.

        One SELECT covering every ID column on ``players``, scoped to
        rows where *any* of those columns is non-NULL — cheap on a 2-3k
        row table, prohibitive to redo per call.
        """
        stmt = select(
            Player.player_id,
            Player.gsis_id,
            Player.sleeper_id,
            Player.nfl_com_player_id,
            Player.espn_id,
            Player.yahoo_id,
        ).where(
            or_(
                Player.gsis_id.isnot(None),
                Player.sleeper_id.isnot(None),
                Player.nfl_com_player_id.isnot(None),
                Player.espn_id.isnot(None),
                Player.yahoo_id.isnot(None),
            )
        )
        for player_id, *values in self._session.execute(stmt).all():
            for kind, value in zip(_EXTERNAL_ID_KINDS, values, strict=True):
                if value:
                    self._direct_index[kind][value] = player_id

    def _get_fuzzy_index(self) -> list[tuple[int, str, str | None]]:
        if self._fuzzy_index is None or self._fuzzy_dirty:
            stmt = select(Player.player_id, Player.name_full, Player.position)
            self._fuzzy_index = [
                (pid, name, pos) for pid, name, pos in self._session.execute(stmt).all()
            ]
            self._fuzzy_dirty = False
        return self._fuzzy_index


def _id_value(identity: PlayerIdentity, kind: str) -> str | None:
    """Extract one external-ID field from a :class:`PlayerIdentity`."""
    return getattr(identity, kind, None)


def _is_nfl_com_team_defense(identity: PlayerIdentity) -> bool:
    """True if ``identity`` carries a team-defense ``nfl_com_player_id``."""
    raw = identity.nfl_com_player_id
    if raw is None:
        return False
    try:
        nfl_id = int(raw)
    except (TypeError, ValueError):
        return False
    return _NFL_COM_DST_ID_MIN <= nfl_id <= _NFL_COM_DST_ID_MAX


def _effective_position(identity: PlayerIdentity) -> str | None:
    """Position to store for ``identity``, correcting known team defenses.

    A team defense scraped while its NFL.com page shows a non-position
    banner arrives with ``position=None`` (``_clean_position`` rejects the
    banner as a position). For the well-known DST id range the position is
    unambiguously "DEF", so supply it rather than storing NULL. All other
    identities pass through unchanged — this only *fills* an unknown
    position, it never overrides one the source actually reported.
    """
    if identity.position is None and _is_nfl_com_team_defense(identity):
        return "DEF"
    return identity.position


__all__ = [
    "FUZZY_MATCH_THRESHOLD",
    "PlayerIdentity",
    "PlayerResolver",
    "ResolveStats",
]
