"""Add player_season_positions

Season-correct NFL position per (player, season). ``players.position`` is a
single current/last-known snapshot that misrepresents any season before a
position change or any plain mislabel (a 2014 WR shown as a later-career TE).
This table is the season-aware counterpart, sourced from nflverse's per-season
rosters and backfilled by ``scripts/backfill_season_positions.py`` (the data
load needs network, so it lives outside this schema-only migration).

Revision ID: a1b3c5d7e9f2
Revises: c9f2a6b8d4e1
Create Date: 2026-06-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b3c5d7e9f2"
down_revision: str | Sequence[str] | None = "c9f2a6b8d4e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "player_season_positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("position", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.player_id"],
            name="fk_player_season_positions_player",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "player_id",
            "season_year",
            name="uq_player_season_positions_player_season",
        ),
    )
    op.create_index(
        "ix_player_season_positions_season",
        "player_season_positions",
        ["season_year"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_player_season_positions_season", table_name="player_season_positions")
    op.drop_table("player_season_positions")
