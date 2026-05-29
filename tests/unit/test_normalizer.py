"""Unit tests for the M7 normalizer.

Covers :mod:`ff_pipeline.normalizer.player_ids` and
:mod:`ff_pipeline.normalizer.conflicts`. Uses an in-memory-style SQLite
DB built from a temp file path so SQLAlchemy's autoincrement /
``RETURNING`` semantics behave the same as production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ff_pipeline.normalizer.conflicts import (
    is_higher_precedence,
    pick_primary,
    precedence,
    priority,
)
from ff_pipeline.normalizer.player_ids import (
    FUZZY_MATCH_THRESHOLD,
    PlayerIdentity,
    PlayerResolver,
)
from ff_pipeline.repository.database import create_app_engine
from ff_pipeline.repository.migrations import upgrade_to_head
from ff_pipeline.repository.models import Player, PlayerIdOverride

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_app_engine(f"sqlite:///{tmp_path / 'norm.db'}")
    upgrade_to_head(engine=engine)
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _seed_player(session: Session, **kwargs: object) -> int:
    """Insert one row into ``players`` and return its player_id."""
    defaults: dict[str, object] = {"is_active": True}
    defaults.update(kwargs)
    if "name_full" not in defaults:
        defaults["name_full"] = "(test)"
    player = Player(**defaults)
    session.add(player)
    session.flush()
    assert isinstance(player.player_id, int)
    return player.player_id


# ---------------------------------------------------------------------------
# conflicts.py
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_precedence_identity_matches_doc(self) -> None:
        assert precedence("identity") == ("nflverse", "nfl_com", "sleeper")

    def test_priority_returns_index(self) -> None:
        assert priority("nflverse", "identity") == 0
        assert priority("nfl_com", "identity") == 1
        assert priority("sleeper", "identity") == 2

    def test_priority_unknown_source(self) -> None:
        assert priority("yahoo", "identity") is None

    def test_higher_precedence_picks_authoritative_winner(self) -> None:
        assert is_higher_precedence("nflverse", "sleeper", "identity") is True
        assert is_higher_precedence("sleeper", "nflverse", "identity") is False

    def test_higher_precedence_no_incumbent(self) -> None:
        assert is_higher_precedence("sleeper", None, "identity") is True

    def test_higher_precedence_unknown_candidate_loses(self) -> None:
        assert is_higher_precedence("totally-made-up", None, "identity") is False

    def test_higher_precedence_equal_rank(self) -> None:
        assert is_higher_precedence("nflverse", "nflverse", "identity") is False

    def test_higher_precedence_unknown_incumbent_yields_to_candidate(self) -> None:
        # If the incumbent isn't authoritative, the candidate should win
        # provided the candidate IS authoritative.
        assert is_higher_precedence("nflverse", "yahoo", "identity") is True

    def test_pick_primary_returns_top(self) -> None:
        assert pick_primary(["sleeper", "nfl_com", "nflverse"], "identity") == "nflverse"

    def test_pick_primary_projections_inverts_identity(self) -> None:
        # Projections precedence puts sleeper first — the inverse of identity.
        assert pick_primary(["sleeper", "nfl_com_api"], "projections") == "sleeper"

    def test_pick_primary_none_when_all_irrelevant(self) -> None:
        assert pick_primary(["yahoo", "twitter"], "projections") is None


# ---------------------------------------------------------------------------
# PlayerResolver — direct ID matching + creation
# ---------------------------------------------------------------------------


class TestResolverDirectMatch:
    def test_creates_when_no_match(self, session: Session) -> None:
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Jamarr Chase",
            position="WR",
            nfl_team="CIN",
            gsis_id="00-0036900",
        )
        pid = resolver.resolve(identity, source="nflverse")
        assert pid > 0
        assert resolver.stats.created == 1
        # Round-trip — should hit the cache, not insert again.
        again = resolver.resolve(identity, source="nflverse")
        assert again == pid
        assert resolver.stats.created == 1
        assert resolver.stats.matched_by_direct_id == 1

    def test_matches_by_gsis_id(self, session: Session) -> None:
        seeded = _seed_player(
            session,
            name_full="Patrick Mahomes",
            position="QB",
            nfl_team="KC",
            gsis_id="00-0033873",
        )
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Patrick Mahomes II",  # different display, same gsis
            position="QB",
            gsis_id="00-0033873",
            sleeper_id="4034",
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert pid == seeded
        # Sleeper merge should have stamped the sleeper_id onto the row.
        player = session.get(Player, seeded)
        assert player is not None
        assert player.sleeper_id == "4034"
        # Sleeper is lower precedence than the unknown incumbent → name
        # is NOT overwritten.
        assert player.name_full == "Patrick Mahomes"

    def test_matches_by_sleeper_id(self, session: Session) -> None:
        seeded = _seed_player(session, name_full="Travis Kelce", position="TE", sleeper_id="421")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Travis Kelce",
            position="TE",
            sleeper_id="421",
            gsis_id="00-0030506",
        )
        pid = resolver.resolve(identity, source="nflverse")
        assert pid == seeded
        # nflverse stamps gsis_id since the existing row had it blank.
        player = session.get(Player, seeded)
        assert player is not None
        assert player.gsis_id == "00-0030506"
        assert resolver.stats.merged_ids_by_kind == {"gsis_id": 1}

    def test_conflicting_external_id_is_not_overwritten(self, session: Session) -> None:
        seeded = _seed_player(session, name_full="Foo Bar", position="RB", gsis_id="00-0099999")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Foo Bar",
            position="RB",
            gsis_id="00-0099999",
            # Sleeper feed lists a DIFFERENT sleeper_id... but say a stale
            # row in the DB already had sleeper_id="A" — we put that in
            # to exercise the conflict path.
            sleeper_id="B",
        )
        # Pre-stamp a conflicting sleeper_id on the seeded row.
        player = session.get(Player, seeded)
        assert player is not None
        player.sleeper_id = "A"
        session.flush()

        # Re-construct the resolver so its cached index reflects the
        # pre-stamped value.
        resolver = PlayerResolver(session)
        pid = resolver.resolve(identity, source="sleeper")
        assert pid == seeded
        # Conflict path: the resolver refuses to overwrite the existing
        # sleeper_id with a different one.
        player = session.get(Player, seeded)
        assert player is not None
        assert player.sleeper_id == "A"


# ---------------------------------------------------------------------------
# PlayerResolver — fuzzy fallback
# ---------------------------------------------------------------------------


class TestResolverFuzzy:
    def test_fuzzy_match_marvin_mims_jr(self, session: Session) -> None:
        """Stubborn case called out in the roadmap.

        The nflverse row exists as "Marvin Mims". A subsequent Sleeper
        observation arrives as "Marvin Mims Jr." with no overlapping
        external IDs (yet). The resolver should fuzzy-match them and
        merge the Sleeper ID onto the existing row.
        """
        seeded = _seed_player(session, name_full="Marvin Mims", position="WR", gsis_id="00-0039000")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Marvin Mims Jr.",
            position="WR",
            sleeper_id="9999",
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert pid == seeded
        assert resolver.stats.matched_by_fuzzy == 1
        player = session.get(Player, seeded)
        assert player is not None
        assert player.sleeper_id == "9999"

    def test_fuzzy_match_dj_moore_punctuation(self, session: Session) -> None:
        """``D.J. Moore`` vs ``DJ Moore`` is a known cross-source disagreement."""
        seeded = _seed_player(session, name_full="D.J. Moore", position="WR", gsis_id="00-0033881")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="DJ Moore",
            position="WR",
            sleeper_id="4866",
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert pid == seeded
        assert resolver.stats.matched_by_fuzzy == 1

    def test_fuzzy_rejects_below_threshold(self, session: Session) -> None:
        """Different player at same position must NOT collapse."""
        existing_pid = _seed_player(
            session, name_full="Mike Williams", position="WR", gsis_id="00-0033900"
        )
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Mike Evans",
            position="WR",
            sleeper_id="2449",
        )
        pid = resolver.resolve(identity, source="sleeper")
        # The threshold rejects → a new player row is created.
        assert pid != existing_pid
        assert resolver.stats.created == 1
        # The fuzzy path may or may not have surfaced a best candidate;
        # either way the new row should be Mike Evans, not Mike Williams.
        player = session.get(Player, pid)
        assert player is not None
        assert player.name_full == "Mike Evans"

    def test_fuzzy_rejects_when_position_differs(self, session: Session) -> None:
        """Same name at different positions are distinct players."""
        _seed_player(session, name_full="Adrian Peterson", position="RB", gsis_id="00-0024287")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Adrian Peterson",
            position="WR",  # different position — different player
            sleeper_id="ap-wr",
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert resolver.stats.created == 1
        assert resolver.stats.matched_by_fuzzy == 0
        player = session.get(Player, pid)
        assert player is not None
        assert player.position == "WR"

    def test_fuzzy_rejects_when_conflicting_id_present(self, session: Session) -> None:
        """If the fuzzy candidate has a different sleeper_id, refuse the merge.

        Otherwise we'd silently collapse two distinct Sleeper-known
        players.
        """
        existing = _seed_player(
            session,
            name_full="Calvin Johnson",
            position="WR",
            gsis_id="00-0026195",
            sleeper_id="123",
        )
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Calvin Johnson",
            position="WR",
            sleeper_id="456",  # different sleeper_id — must NOT match
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert pid != existing
        assert resolver.stats.fuzzy_rejected_conflicting_id == 1
        assert resolver.stats.created == 1

    def test_fuzzy_threshold_constant_is_high_enough(self) -> None:
        # Sanity guard: if someone drops the threshold, this test fires.
        assert FUZZY_MATCH_THRESHOLD >= 80


# ---------------------------------------------------------------------------
# PlayerResolver — overrides
# ---------------------------------------------------------------------------


class TestResolverOverrides:
    def test_override_short_circuits_fuzzy(self, session: Session) -> None:
        # Two distinct Marvin Mims-likes that fuzzy would otherwise
        # collapse. The override pins the Sleeper ID to the *junior* row.
        senior = _seed_player(session, name_full="Marvin Mims", position="WR", gsis_id="00-OLD")
        junior = _seed_player(session, name_full="Marvin Mims Jr.", position="WR", gsis_id="00-NEW")
        session.add(
            PlayerIdOverride(
                external_id_kind="sleeper_id",
                external_id_value="9999",
                player_id=junior,
                notes="Pin Sleeper 9999 to the JR row",
            )
        )
        session.flush()

        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Marvin Mims",  # would fuzzy-match the senior row
            position="WR",
            sleeper_id="9999",
        )
        pid = resolver.resolve(identity, source="sleeper")
        assert pid == junior
        assert pid != senior
        assert resolver.stats.matched_by_override == 1

    def test_override_stamps_external_ids_onto_pinned_row(self, session: Session) -> None:
        pinned = _seed_player(session, name_full="Manual Pin", position="RB", gsis_id="00-PIN")
        session.add(
            PlayerIdOverride(
                external_id_kind="sleeper_id",
                external_id_value="X42",
                player_id=pinned,
            )
        )
        session.flush()

        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Manual Pin",
            position="RB",
            sleeper_id="X42",
            espn_id="E42",
        )
        resolver.resolve(identity, source="sleeper")
        player = session.get(Player, pinned)
        assert player is not None
        assert player.sleeper_id == "X42"
        assert player.espn_id == "E42"


# ---------------------------------------------------------------------------
# PlayerResolver — try_match
# ---------------------------------------------------------------------------


class TestResolverTryMatch:
    def test_try_match_returns_none_on_miss(self, session: Session) -> None:
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(name_full="Nobody Here", position="QB", sleeper_id="00")
        assert resolver.try_match(identity, source="sleeper") is None
        assert resolver.stats.created == 0
        # No row should have been inserted.
        rows = session.execute(select(Player)).scalars().all()
        assert rows == []

    def test_try_match_does_merge_on_hit(self, session: Session) -> None:
        pid = _seed_player(session, name_full="Hit", gsis_id="00-HIT")
        resolver = PlayerResolver(session)
        identity = PlayerIdentity(
            name_full="Hit",
            gsis_id="00-HIT",
            sleeper_id="hit-sleeper",
            espn_id="hit-espn",
        )
        matched = resolver.try_match(identity, source="sleeper")
        assert matched == pid
        player = session.get(Player, pid)
        assert player is not None
        assert player.sleeper_id == "hit-sleeper"
        assert player.espn_id == "hit-espn"
        assert resolver.stats.merged_ids_by_kind == {"sleeper_id": 1, "espn_id": 1}


# ---------------------------------------------------------------------------
# PlayerResolver — cross-source query equivalence
# ---------------------------------------------------------------------------


class TestResolverEndToEnd:
    def test_query_by_any_id_returns_same_player(self, session: Session) -> None:
        """M7 "Done when" — querying by GSIS, Sleeper, or NFL.com ID all hit
        the same internal player_id once the resolver has merged sources."""
        resolver = PlayerResolver(session)

        # nflverse arrival first: gsis only.
        resolver.resolve(
            PlayerIdentity(
                name_full="Justin Jefferson",
                position="WR",
                nfl_team="MIN",
                gsis_id="00-0036322",
            ),
            source="nflverse",
        )
        # NFL.com arrival next: nfl_com_player_id + name only.
        resolver.resolve(
            PlayerIdentity(
                name_full="Justin Jefferson",
                position="WR",
                nfl_com_player_id="2569790",
            ),
            source="nfl_com",
        )
        # Sleeper last: sleeper_id + gsis_id.
        resolver.resolve(
            PlayerIdentity(
                name_full="Justin Jefferson",
                position="WR",
                gsis_id="00-0036322",
                sleeper_id="7553",
                espn_id="4262921",
            ),
            source="sleeper",
        )

        by_gsis = session.execute(
            select(Player.player_id).where(Player.gsis_id == "00-0036322")
        ).scalar_one()
        by_sleeper = session.execute(
            select(Player.player_id).where(Player.sleeper_id == "7553")
        ).scalar_one()
        by_nfl_com = session.execute(
            select(Player.player_id).where(Player.nfl_com_player_id == "2569790")
        ).scalar_one()
        assert by_gsis == by_sleeper == by_nfl_com
