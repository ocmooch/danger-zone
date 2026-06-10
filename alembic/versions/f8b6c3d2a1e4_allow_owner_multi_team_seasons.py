"""Allow one owner to manage multiple teams in a season

Revision ID: f8b6c3d2a1e4
Revises: e6f4a2b8c9d1
Create Date: 2026-06-07 00:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b6c3d2a1e4"
down_revision: str | Sequence[str] | None = "e6f4a2b8c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_constraint("uq_teams_season_owner", type_="unique")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("teams") as batch_op:
        batch_op.create_unique_constraint(
            "uq_teams_season_owner",
            ["season_id", "owner_id"],
        )
