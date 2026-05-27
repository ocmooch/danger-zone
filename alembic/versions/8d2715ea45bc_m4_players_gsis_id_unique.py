"""M4: players gsis_id unique

Replaces the plain ``ix_players_gsis_id`` index with a UNIQUE constraint so
the nflverse crawler can upsert players via ``ON CONFLICT (gsis_id)``.
gsis_id stays nullable: players first seen on NFL.com (before M7's
normalizer links them to nflverse) land with a NULL gsis_id, which both
SQLite and PostgreSQL allow under a UNIQUE constraint.

Revision ID: 8d2715ea45bc
Revises: 308e7f4e6cfd
Create Date: 2026-05-27 13:12:06.478257
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d2715ea45bc"
down_revision: str | Sequence[str] | None = "308e7f4e6cfd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_players_gsis_id"))
        batch_op.create_unique_constraint("uq_players_gsis_id", ["gsis_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("players", schema=None) as batch_op:
        batch_op.drop_constraint("uq_players_gsis_id", type_="unique")
        batch_op.create_index(batch_op.f("ix_players_gsis_id"), ["gsis_id"], unique=False)
