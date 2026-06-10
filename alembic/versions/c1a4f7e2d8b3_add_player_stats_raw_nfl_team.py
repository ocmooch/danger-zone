"""Add player_stats_raw.nfl_team

Persists the player's own NFL team for the week the stat line covers — the
season-correct counterpart to the single current snapshot on
``players.nfl_team``. nflverse already ships this per-week ``team`` (it feeds
the same ``NflversePlayerStat`` rows as ``nfl_opponent``); it was previously
dropped before persistence. Storing it lets season-scoped surfaces (historical
leaderboards, rosters) render the team a player was on *that* season instead of
their current one. Nullable: unknown for non-nflverse sources that omit it, and
NULL on pre-existing rows until the nflverse ingest is re-run to backfill it.

Revision ID: c1a4f7e2d8b3
Revises: f8b6c3d2a1e4
Create Date: 2026-06-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a4f7e2d8b3"
down_revision: str | Sequence[str] | None = "f8b6c3d2a1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("player_stats_raw", schema=None) as batch_op:
        batch_op.add_column(sa.Column("nfl_team", sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("player_stats_raw", schema=None) as batch_op:
        batch_op.drop_column("nfl_team")
