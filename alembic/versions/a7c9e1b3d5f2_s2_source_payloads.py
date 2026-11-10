"""S2 source_payloads: index of the raw Sleeper league payload archive

Adds the ``source_payloads`` table (docs/plans/S2-sleeper-league-capture.md §4).
Additive only: one new table, no existing table or value changes, and no
legacy guard trigger (the table holds no sealed-season rows).

Revision ID: a7c9e1b3d5f2
Revises: e2a4c6e8f0b1
Create Date: 2026-11-09 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c9e1b3d5f2"
down_revision: str | Sequence[str] | None = "e2a4c6e8f0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_payloads",
        sa.Column("payload_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=32), nullable=False),
        sa.Column("platform_league_id", sa.String(), nullable=True),
        sa.Column("season_year", sa.Integer(), nullable=True),
        sa.Column("week", sa.Integer(), nullable=True),
        sa.Column("request_url", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("first_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("payload_id"),
        sa.UniqueConstraint("sha256", "request_url", name="uq_source_payloads_sha256_url"),
    )
    op.create_index(
        "ix_source_payloads_url_last",
        "source_payloads",
        ["request_url", "last_fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_payloads_endpoint",
        "source_payloads",
        ["source", "endpoint", "season_year", "week"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_source_payloads_endpoint", table_name="source_payloads")
    op.drop_index("ix_source_payloads_url_last", table_name="source_payloads")
    op.drop_table("source_payloads")
