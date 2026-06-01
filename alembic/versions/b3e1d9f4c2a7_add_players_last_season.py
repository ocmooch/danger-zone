"""Add players.last_season

Captures nflverse ``load_players().last_season`` — the last NFL season a
player appeared in. Used to scope ingestion to the league era so players
who retired before ``LEAGUE_START_YEAR`` are never (re-)added to the
``players`` table. Nullable: unknown for non-nflverse-sourced rows (e.g.
players first seen on NFL.com) and for the rare nflverse row that omits it.

Revision ID: b3e1d9f4c2a7
Revises: a7f3c0e9b211
Create Date: 2026-06-01 19:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e1d9f4c2a7"
down_revision: str | Sequence[str] | None = "a7f3c0e9b211"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_season", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.drop_column("last_season")
