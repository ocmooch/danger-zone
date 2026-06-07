"""Owner identity override table

Creates ``owner_identity_overrides``, a small manual-curation table that pins
historical owner display names or NFL.com user IDs to one canonical manager
identity. This lets reconstruction keep one ``owners`` row per human even when
NFL.com exposes multiple names or account IDs for the same person.

Revision ID: e6f4a2b8c9d1
Revises: b2c3d4e5f6a7
Create Date: 2026-06-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f4a2b8c9d1"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "owner_identity_overrides",
        sa.Column("override_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("league_id", sa.String(), nullable=False),
        sa.Column("external_id_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id_value", sa.String(), nullable=False),
        sa.Column("canonical_display_name", sa.String(), nullable=False),
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
            ["league_id"],
            ["leagues.league_id"],
            name="fk_owner_identity_overrides_league",
        ),
        sa.PrimaryKeyConstraint("override_id"),
        sa.UniqueConstraint(
            "league_id",
            "external_id_kind",
            "external_id_value",
            name="uq_owner_identity_overrides_league_kind_value",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("owner_identity_overrides")
