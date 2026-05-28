"""M7: player_id_overrides table

Creates ``player_id_overrides``, a small manual-curation table the M7
normalizer consults *before* doing direct-ID or fuzzy matching. Each row
pins one external ID (``gsis_id`` / ``sleeper_id`` / ``nfl_com_player_id`` /
``espn_id`` / ``yahoo_id``) to a specific internal ``player_id``, e.g.
to disambiguate the canonical "Marvin Mims" row from a "Marvin Mims Jr."
duplicate that fuzzy matching keeps confusing.

Revision ID: a7f3c0e9b211
Revises: 5cfbbf4a868f
Create Date: 2026-05-28 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f3c0e9b211"
down_revision: str | Sequence[str] | None = "5cfbbf4a868f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "player_id_overrides",
        sa.Column("override_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id_value", sa.String(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
            name="fk_player_id_overrides_player",
        ),
        sa.PrimaryKeyConstraint("override_id"),
        sa.UniqueConstraint(
            "external_id_kind",
            "external_id_value",
            name="uq_player_id_overrides_kind_value",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("player_id_overrides")
