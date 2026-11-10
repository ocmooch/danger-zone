"""Add player_adp

Average Draft Position per (season, source, source-player) — the *market* axis
behind the draft surfaces (reach vs value). Rows are stored raw and
source-faithful (FFC / MFL / Sleeper); the weighted multi-source blend and the
reach/value delta are computed downstream in the dashboard so the weighting
stays tunable without a re-ingest. ``player_id`` is nullable so an unresolved
source player is still stored (kept for re-match + coverage audit). The actual
ADP data load needs network, so it lives in the crawler/backfill, not here
(schema-only migration), mirroring ``player_season_positions``.

Revision ID: d4e6f8a0b2c4
Revises: a1b3c5d7e9f2
Create Date: 2026-06-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e6f8a0b2c4"
down_revision: str | Sequence[str] | None = "a1b3c5d7e9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "player_adp",
        sa.Column("adp_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_player_key", sa.String(), nullable=False),
        sa.Column("source_player_name", sa.String(), nullable=True),
        sa.Column("source_position", sa.String(length=8), nullable=True),
        sa.Column("source_nfl_team", sa.String(length=8), nullable=True),
        sa.Column("requested_format", sa.String(length=16), nullable=False),
        sa.Column("actual_format", sa.String(length=16), nullable=False),
        sa.Column("format_fallback", sa.Boolean(), nullable=False),
        sa.Column("teams", sa.Integer(), nullable=True),
        sa.Column("adp", sa.Float(), nullable=False),
        sa.Column("adp_stdev", sa.Float(), nullable=True),
        sa.Column("adp_high", sa.Float(), nullable=True),
        sa.Column("adp_low", sa.Float(), nullable=True),
        sa.Column("times_drafted", sa.Integer(), nullable=True),
        sa.Column(
            "pulled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("run_id", sa.Integer(), nullable=True),
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
            ["season_id"], ["seasons.season_id"], name="fk_player_adp_season"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.player_id"], name="fk_player_adp_player"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["pipeline_runs.run_id"], name="fk_player_adp_run"
        ),
        sa.PrimaryKeyConstraint("adp_id"),
        sa.UniqueConstraint(
            "season_id",
            "source",
            "source_player_key",
            name="uq_player_adp_season_source_key",
        ),
    )
    op.create_index("ix_player_adp_season", "player_adp", ["season_id"])
    op.create_index("ix_player_adp_player", "player_adp", ["player_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_player_adp_player", table_name="player_adp")
    op.drop_index("ix_player_adp_season", table_name="player_adp")
    op.drop_table("player_adp")
