"""Add players.first_rostered_season / last_rostered_season

Materializes each player's league-relevance span: the MIN/MAX
``team_rosters.season_year`` they appear on in *this* league. NULL ⇒ the
player was never rostered here — the queryable "league-relevant?" signal that
``last_season`` (a current-NFL fact from nflverse) cannot provide, since
nflverse ships the entire NFL player universe and most of it never touched
this league. The read API filters on these and surfaces a "rostered
2012-2018" span without a per-request join.

The upgrade backfills both columns from the existing ``team_rosters`` rows so
a DB migrating up is immediately correct; the NFL.com roster runner recomputes
them after each sync to keep them fresh.

Revision ID: f7a2b4c8d1e3
Revises: d5a2c7b9f3e1
Create Date: 2026-06-02 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a2b4c8d1e3"
down_revision: str | Sequence[str] | None = "d5a2c7b9f3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.add_column(sa.Column("first_rostered_season", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_rostered_season", sa.Integer(), nullable=True))

    # Backfill from existing roster rows. Players with no team_rosters row keep
    # NULL spans (never rostered = not league-relevant). Correlated subqueries
    # are portable across SQLite and PostgreSQL.
    op.execute(
        """
        UPDATE players
        SET first_rostered_season = (
                SELECT MIN(tr.season_year) FROM team_rosters tr
                WHERE tr.player_id = players.player_id
            ),
            last_rostered_season = (
                SELECT MAX(tr.season_year) FROM team_rosters tr
                WHERE tr.player_id = players.player_id
            )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.drop_column("last_rostered_season")
        batch_op.drop_column("first_rostered_season")
