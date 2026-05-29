"""M6: trending_players table

Creates ``trending_players`` to capture Sleeper's per-fetch adds/drops
trending data. One row per (player, trend_type, lookback_hours, fetched_at)
so historical trend snapshots are preserved.

Revision ID: 5cfbbf4a868f
Revises: 8d2715ea45bc
Create Date: 2026-05-27 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5cfbbf4a868f"
down_revision: str | Sequence[str] | None = "8d2715ea45bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "trending_players",
        sa.Column("trending_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("trend_type", sa.String(length=16), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("lookback_hours", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.player_id"],
            name="fk_trending_players_player",
        ),
        sa.PrimaryKeyConstraint("trending_id"),
        sa.UniqueConstraint(
            "player_id",
            "trend_type",
            "lookback_hours",
            "fetched_at",
            name="uq_trending_players_player_type_lookback_fetched",
        ),
    )
    with op.batch_alter_table("trending_players", schema=None) as batch_op:
        batch_op.create_index(
            "ix_trending_players_fetched_type",
            ["fetched_at", "trend_type"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("trending_players", schema=None) as batch_op:
        batch_op.drop_index("ix_trending_players_fetched_type")
    op.drop_table("trending_players")
