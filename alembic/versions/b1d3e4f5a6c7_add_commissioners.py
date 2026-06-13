"""add commissioners

Creates ``commissioners``, a small manually-curated table holding one row per
commissioner term (``from_year``/``to_year`` inclusive NFL season years;
``to_year`` NULL = ongoing). Seeded from ``data/commissioner_history.yaml`` via
``scripts/load_commissioner_history.py``. No data is inserted here.

Revision ID: b1d3e4f5a6c7
Revises: 407521092d7e
Create Date: 2026-06-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d3e4f5a6c7"
down_revision: str | Sequence[str] | None = "407521092d7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "commissioners",
        sa.Column("commissioner_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("league_id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("from_year", sa.Integer(), nullable=False),
        sa.Column("to_year", sa.Integer(), nullable=True),
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
            name="fk_commissioners_league",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["owners.owner_id"],
            name="fk_commissioners_owner",
        ),
        sa.PrimaryKeyConstraint("commissioner_id"),
        sa.UniqueConstraint(
            "league_id",
            "owner_id",
            "from_year",
            name="uq_commissioners_owner_from",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("commissioners")
