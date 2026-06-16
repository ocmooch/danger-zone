"""Add player identity link crosswalk

Creates an auditable member-to-canonical crosswalk for split ``players`` rows.
This is additive: no existing foreign keys are rewritten and no player rows are
deleted.

Revision ID: c9f2a6b8d4e1
Revises: b1d3e4f5a6c7
Create Date: 2026-06-16 17:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f2a6b8d4e1"
down_revision: str | Sequence[str] | None = "b1d3e4f5a6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "player_identity_links",
        sa.Column("link_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("member_player_id", sa.Integer(), nullable=False),
        sa.Column("canonical_player_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
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
            ["canonical_player_id"],
            ["players.player_id"],
            name="fk_player_identity_links_canonical",
        ),
        sa.ForeignKeyConstraint(
            ["member_player_id"],
            ["players.player_id"],
            name="fk_player_identity_links_member",
        ),
        sa.PrimaryKeyConstraint("link_id"),
        sa.UniqueConstraint(
            "member_player_id",
            name="uq_player_identity_links_member",
        ),
    )
    op.create_index(
        "ix_player_identity_links_canonical",
        "player_identity_links",
        ["canonical_player_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_player_identity_links_canonical", table_name="player_identity_links")
    op.drop_table("player_identity_links")
