"""Add transactions.extra_data

Adds a JSON payload column to ``transactions`` so the full chronological
league diary can be captured: lineup/start-sit moves carry their slot
detail ({"from_slot", "to_slot"}) and commissioner/league-setting changes
carry their description, neither of which fits the player-move columns.
Nullable — NULL for ordinary add/drop/trade rows.

Revision ID: a1b2c3d4e5f6
Revises: f7a2b4c8d1e3
Create Date: 2026-06-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f7a2b4c8d1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extra_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_column("extra_data")
