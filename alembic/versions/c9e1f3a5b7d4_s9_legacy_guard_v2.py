"""S9: legacy guard v2 — Sleeper-era columns and playoff_brackets for sealed seasons.

Adds the triggers of ``ff_pipeline.legacy.triggers.trigger_ddl_v2`` (S9-D6). A
literal copy, so the migration stays frozen; ``tests/unit/test_legacy_triggers.py``
asserts it equals the generator. Native ``CREATE TRIGGER`` only, never a batch
rebuild (a batch rebuild would drop the S1 triggers). Inert until a Sleeper
season is sealed; NFL.com rows carry no values in these columns.

Revision ID: c9e1f3a5b7d4
Revises: b8d0f2a4c6e3
Create Date: 2026-11-10
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c9e1f3a5b7d4"
down_revision: str | Sequence[str] | None = "b8d0f2a4c6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS: list[tuple[str, str]] = [
    (
        "legacy_guard_v2_matchups_update",
        "CREATE TRIGGER legacy_guard_v2_matchups_update BEFORE UPDATE ON matchups\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.platform_matchup_key IS NOT OLD.platform_matchup_key OR NEW.extra_data IS NOT OLD.extra_data)\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: matchups row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_playoff_brackets_insert",
        "CREATE TRIGGER legacy_guard_v2_playoff_brackets_insert BEFORE INSERT ON playoff_brackets\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: playoff_brackets row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_playoff_brackets_update",
        "CREATE TRIGGER legacy_guard_v2_playoff_brackets_update BEFORE UPDATE ON playoff_brackets\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.bracket_id IS NOT OLD.bracket_id OR NEW.season_id IS NOT OLD.season_id OR NEW.bracket IS NOT OLD.bracket OR NEW.round IS NOT OLD.round OR NEW.match_no IS NOT OLD.match_no OR NEW.team1_id IS NOT OLD.team1_id OR NEW.team2_id IS NOT OLD.team2_id OR NEW.winner_team_id IS NOT OLD.winner_team_id OR NEW.loser_team_id IS NOT OLD.loser_team_id OR NEW.placement IS NOT OLD.placement OR NEW.team1_from IS NOT OLD.team1_from OR NEW.team2_from IS NOT OLD.team2_from OR NEW.source IS NOT OLD.source)\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: playoff_brackets row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_playoff_brackets_delete",
        "CREATE TRIGGER legacy_guard_v2_playoff_brackets_delete BEFORE DELETE ON playoff_brackets\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: playoff_brackets row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_seasons_update",
        "CREATE TRIGGER legacy_guard_v2_seasons_update BEFORE UPDATE ON seasons\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND OLD.is_sealed = 1 AND (NEW.platform IS NOT OLD.platform OR NEW.platform_league_id IS NOT OLD.platform_league_id OR NEW.platform_draft_id IS NOT OLD.platform_draft_id OR NEW.settings_snapshot IS NOT OLD.settings_snapshot)\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: season is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_teams_update",
        "CREATE TRIGGER legacy_guard_v2_teams_update BEFORE UPDATE ON teams\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.platform_team_key IS NOT OLD.platform_team_key OR NEW.division IS NOT OLD.division OR NEW.extra_data IS NOT OLD.extra_data)\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: teams row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_v2_transactions_update",
        "CREATE TRIGGER legacy_guard_v2_transactions_update BEFORE UPDATE ON transactions\nWHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.platform_transaction_id IS NOT OLD.platform_transaction_id OR NEW.status IS NOT OLD.status)\nBEGIN SELECT RAISE(ABORT, 'legacy_guard: transactions row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    for _name, sql in TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    for name, _sql in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
