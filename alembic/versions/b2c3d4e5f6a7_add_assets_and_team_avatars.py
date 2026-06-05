"""Add assets table + teams avatar FKs

Preserves team/owner avatars as bytes-on-disk + metadata-in-DB. The new
``assets`` table is content-addressed (UNIQUE ``sha256`` dedupes identical
default avatars); raw bytes live under ``data/assets/`` (gitignored), not in
the DB. ``teams`` gains ``team_avatar_asset_id`` / ``owner_avatar_asset_id``
FKs so each per-season team row snapshots the logo as it appeared then.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-05 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("league_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
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
            ["league_id"], ["leagues.league_id"], name="fk_assets_league"
        ),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("sha256", name="uq_assets_sha256"),
    )
    op.create_index("ix_assets_league", "assets", ["league_id"], unique=False)

    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.add_column(sa.Column("team_avatar_asset_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("owner_avatar_asset_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_teams_team_avatar", "assets", ["team_avatar_asset_id"], ["asset_id"]
        )
        batch_op.create_foreign_key(
            "fk_teams_owner_avatar", "assets", ["owner_avatar_asset_id"], ["asset_id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.drop_constraint("fk_teams_owner_avatar", type_="foreignkey")
        batch_op.drop_constraint("fk_teams_team_avatar", type_="foreignkey")
        batch_op.drop_column("owner_avatar_asset_id")
        batch_op.drop_column("team_avatar_asset_id")

    op.drop_index("ix_assets_league", table_name="assets")
    op.drop_table("assets")
