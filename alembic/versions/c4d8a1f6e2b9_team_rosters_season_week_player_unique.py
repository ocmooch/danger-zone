"""team_rosters: season-scoped, one-team-per-player-per-week unique key

Replaces the old ``uq_team_rosters_team_player_week`` (``team_id``,
``player_id``, ``week``) constraint with ``uq_team_rosters_season_week_player``
(``season_year``, ``week``, ``player_id``).

Two problems with the old key:
  * it omitted ``season_year`` entirely, and
  * being keyed on ``team_id`` it permitted the SAME player on DIFFERENT teams
    in one week — the invariant that broke for 2025 week 1, where 36 players
    who moved between two roster snapshots ended up double-rostered.

The new key drops ``team_id`` so a player can occupy at most one team in a
given scoring week, enforced at the DB level.

IMPORTANT — ordering: this migration adds a UNIQUE constraint that the CURRENT
2025-week-1 data VIOLATES. It will fail with an IntegrityError unless the
corrupt rows are repaired first (see FIX_THIS.txt PART 2: back up the db,
DELETE the 2025 wk1 team_rosters rows, re-ingest under the fixed loader). Run
the data repair, THEN ``alembic upgrade head``.

Revision ID: c4d8a1f6e2b9
Revises: b3e1d9f4c2a7
Create Date: 2026-06-02 01:40:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8a1f6e2b9"
down_revision: str | Sequence[str] | None = "b3e1d9f4c2a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("team_rosters", schema=None) as batch_op:
        batch_op.drop_constraint("uq_team_rosters_team_player_week", type_="unique")
        batch_op.create_unique_constraint(
            "uq_team_rosters_season_week_player",
            ["season_year", "week", "player_id"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("team_rosters", schema=None) as batch_op:
        batch_op.drop_constraint("uq_team_rosters_season_week_player", type_="unique")
        batch_op.create_unique_constraint(
            "uq_team_rosters_team_player_week",
            ["team_id", "player_id", "week"],
        )
