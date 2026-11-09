"""S3 platform-era schema: Sleeper-season columns and tables

Additive only (docs/plans/S3-platform-era-schema.md, ``03`` §9): new
nullable/defaulted columns on ``seasons``, ``teams``, ``matchups``,
``transactions`` and ``players``; five new tables; partial unique indexes.
No existing value changes; legacy rows read ``seasons.platform = 'nfl_com'``.

**Never batch.** ``alembic/env.py`` renders batch mode, and a batch operation
rebuilds the table, which silently drops its S1 legacy-guard triggers. Columns
are added with native ``ALTER TABLE ADD COLUMN`` and removed with native
``ALTER TABLE DROP COLUMN`` (SQLite >= 3.35), so the triggers survive both
directions (S3-D1; tests/integration/test_migration_s3.py).

Revision ID: b8d0f2a4c6e3
Revises: a7c9e1b3d5f2
Create Date: 2026-11-09 00:00:00.000000
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d0f2a4c6e3"
down_revision: str | Sequence[str] | None = "a7c9e1b3d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_COLUMNS: list[tuple[str, sa.Column[object]]] = [
    (
        "seasons",
        sa.Column(
            "platform", sa.String(length=16), server_default=sa.text("'nfl_com'"), nullable=False
        ),
    ),
    ("seasons", sa.Column("platform_league_id", sa.String(), nullable=True)),
    ("seasons", sa.Column("platform_draft_id", sa.String(), nullable=True)),
    ("seasons", sa.Column("settings_snapshot", sa.JSON(), nullable=True)),
    ("teams", sa.Column("platform_team_key", sa.String(), nullable=True)),
    ("teams", sa.Column("division", sa.String(), nullable=True)),
    ("teams", sa.Column("extra_data", sa.JSON(), nullable=True)),
    ("matchups", sa.Column("platform_matchup_key", sa.String(), nullable=True)),
    ("matchups", sa.Column("extra_data", sa.JSON(), nullable=True)),
    ("transactions", sa.Column("platform_transaction_id", sa.String(), nullable=True)),
    ("transactions", sa.Column("status", sa.String(length=16), nullable=True)),
    ("players", sa.Column("extra_data", sa.JSON(), nullable=True)),
]

NEW_TABLES = [
    "legacy_nfl_com_2026_preseason",
    "player_status_snapshots",
    "traded_picks",
    "playoff_brackets",
    "owner_platform_identities",
]


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        op.add_column(table, column)  # native ADD COLUMN: no rebuild, triggers kept

    op.create_table(
        "owner_platform_identities",
        sa.Column("identity_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("external_user_id", sa.String(), nullable=False),
        sa.Column("external_display_name", sa.String(), nullable=True),
        sa.Column("first_season", sa.Integer(), nullable=True),
        sa.Column("last_season", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["owners.owner_id"], name="fk_owner_platform_identities_owner"
        ),
        sa.PrimaryKeyConstraint("identity_id"),
        sa.UniqueConstraint(
            "platform", "external_user_id", name="uq_owner_platform_identities_platform_user"
        ),
    )
    op.create_index("ix_owner_platform_identities_owner", "owner_platform_identities", ["owner_id"])

    op.create_table(
        "playoff_brackets",
        sa.Column("bracket_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("bracket", sa.String(length=16), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("match_no", sa.Integer(), nullable=False),
        sa.Column("team1_id", sa.Integer(), nullable=True),
        sa.Column("team2_id", sa.Integer(), nullable=True),
        sa.Column("winner_team_id", sa.Integer(), nullable=True),
        sa.Column("loser_team_id", sa.Integer(), nullable=True),
        sa.Column("placement", sa.Integer(), nullable=True),
        sa.Column("team1_from", sa.JSON(), nullable=True),
        sa.Column("team2_from", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.season_id"], name="fk_playoff_brackets_season"
        ),
        sa.ForeignKeyConstraint(["team1_id"], ["teams.team_id"], name="fk_playoff_brackets_team1"),
        sa.ForeignKeyConstraint(["team2_id"], ["teams.team_id"], name="fk_playoff_brackets_team2"),
        sa.ForeignKeyConstraint(
            ["winner_team_id"], ["teams.team_id"], name="fk_playoff_brackets_winner"
        ),
        sa.ForeignKeyConstraint(
            ["loser_team_id"], ["teams.team_id"], name="fk_playoff_brackets_loser"
        ),
        sa.PrimaryKeyConstraint("bracket_id"),
        sa.UniqueConstraint(
            "season_id", "bracket", "round", "match_no", name="uq_playoff_brackets_match"
        ),
    )

    op.create_table(
        "traded_picks",
        sa.Column("traded_pick_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("league_id", sa.String(), nullable=False),
        sa.Column("pick_season", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("original_owner_id", sa.Integer(), nullable=False),
        sa.Column("current_owner_id", sa.Integer(), nullable=False),
        sa.Column("previous_owner_id", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_transaction_id", sa.String(), nullable=True),
        sa.Column("extra_data", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["league_id"], ["leagues.league_id"], name="fk_traded_picks_league"
        ),
        sa.ForeignKeyConstraint(
            ["original_owner_id"], ["owners.owner_id"], name="fk_traded_picks_original_owner"
        ),
        sa.ForeignKeyConstraint(
            ["current_owner_id"], ["owners.owner_id"], name="fk_traded_picks_current_owner"
        ),
        sa.ForeignKeyConstraint(
            ["previous_owner_id"], ["owners.owner_id"], name="fk_traded_picks_previous_owner"
        ),
        sa.PrimaryKeyConstraint("traded_pick_id"),
        sa.UniqueConstraint(
            "league_id", "pick_season", "round", "original_owner_id", name="uq_traded_picks_pick"
        ),
    )

    op.create_table(
        "player_status_snapshots",
        sa.Column("snapshot_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("injury_status", sa.String(length=32), nullable=True),
        sa.Column("injury_body_part", sa.String(length=64), nullable=True),
        sa.Column("injury_start_date", sa.Date(), nullable=True),
        sa.Column("practice_participation", sa.String(length=32), nullable=True),
        sa.Column("depth_chart_position", sa.String(length=16), nullable=True),
        sa.Column("depth_chart_order", sa.Integer(), nullable=True),
        sa.Column("nfl_team", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("extra_data", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.player_id"], name="fk_player_status_snapshots_player"
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "player_id", "as_of_date", "source", name="uq_player_status_snapshots_day"
        ),
    )

    op.create_table(
        "legacy_nfl_com_2026_preseason",
        sa.Column("quarantine_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("original_pk", sa.String(), nullable=False),
        sa.Column("row_data", sa.JSON(), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amendment_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["amendment_id"],
            ["legacy_amendments.amendment_id"],
            name="fk_legacy_nfl_com_2026_preseason_amendment",
        ),
        sa.PrimaryKeyConstraint("quarantine_id"),
        sa.UniqueConstraint(
            "source_table", "original_pk", name="uq_legacy_nfl_com_2026_preseason_row"
        ),
    )

    op.create_index("ix_seasons_platform", "seasons", ["platform", "year"])
    op.create_index("ix_matchups_platform_key", "matchups", ["platform_matchup_key"])
    op.create_index(
        "uq_teams_season_platform_key",
        "teams",
        ["season_id", "platform_team_key"],
        unique=True,
        sqlite_where=sa.text("platform_team_key IS NOT NULL"),
        postgresql_where=sa.text("platform_team_key IS NOT NULL"),
    )
    op.create_index(
        "uq_transactions_platform_leg",
        "transactions",
        [
            "season_id",
            "platform_transaction_id",
            sa.text("coalesce(player_id, -1)"),
            sa.text("coalesce(team_id, -1)"),
            sa.text("coalesce(direction, '')"),
        ],
        unique=True,
        sqlite_where=sa.text("platform_transaction_id IS NOT NULL"),
        postgresql_where=sa.text("platform_transaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            "S3 downgrade needs SQLite >= 3.35 (native DROP COLUMN); a batch rebuild "
            "would drop the legacy-guard triggers."
        )
    op.drop_index("uq_transactions_platform_leg", table_name="transactions")
    op.drop_index("uq_teams_season_platform_key", table_name="teams")
    op.drop_index("ix_matchups_platform_key", table_name="matchups")
    op.drop_index("ix_seasons_platform", table_name="seasons")
    op.drop_index("ix_owner_platform_identities_owner", table_name="owner_platform_identities")
    for table in NEW_TABLES:
        op.drop_table(table)
    for table, column in reversed(NEW_COLUMNS):
        # Native DROP COLUMN (not op.drop_column, which batch mode may rebuild).
        op.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column.name}"')
