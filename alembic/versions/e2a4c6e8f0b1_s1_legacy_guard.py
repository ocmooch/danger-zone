"""S1 legacy guard: seal columns, guard tables and triggers

Adds ``seasons.is_sealed / sealed_at / seal_id``, the guard's own tables
(``legacy_guard``, ``legacy_seals``, ``legacy_sealed_rows``,
``legacy_amendments``) and the SQLite guard triggers. Additive only: no
existing value changes and no season is sealed here — sealing is the separate
``ff-pipeline legacy seal --adopt`` step (docs/plans/S1-legacy-guard.md).

The trigger SQL is a frozen literal copy of ``ff_pipeline.legacy.triggers``
output; ``tests/unit/test_legacy_triggers.py`` asserts they stay equal.
Triggers are SQLite-only (Postgres would need trigger functions; not built).

Revision ID: e2a4c6e8f0b1
Revises: d4e6f8a0b2c4
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a4c6e8f0b1"
down_revision: str | Sequence[str] | None = "d4e6f8a0b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS: list[tuple[str, str]] = [
    (
        "legacy_guard_commissioners_update",
        "CREATE TRIGGER legacy_guard_commissioners_update BEFORE UPDATE ON commissioners\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'commissioners' AND r.pk = CAST(OLD.commissioner_id AS TEXT)) AND (NEW.commissioner_id IS NOT OLD.commissioner_id OR NEW.league_id IS NOT OLD.league_id OR NEW.owner_id IS NOT OLD.owner_id OR NEW.from_year IS NOT OLD.from_year OR NEW.to_year IS NOT OLD.to_year OR NEW.notes IS NOT OLD.notes)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: commissioners row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_commissioners_delete",
        "CREATE TRIGGER legacy_guard_commissioners_delete BEFORE DELETE ON commissioners\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'commissioners' AND r.pk = CAST(OLD.commissioner_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: commissioners row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_leagues_update",
        "CREATE TRIGGER legacy_guard_leagues_update BEFORE UPDATE ON leagues\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'leagues' AND r.pk = CAST(OLD.league_id AS TEXT)) AND (NEW.league_id IS NOT OLD.league_id OR NEW.name IS NOT OLD.name OR NEW.platform IS NOT OLD.platform OR NEW.current_season_year IS NOT OLD.current_season_year)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: leagues row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_leagues_delete",
        "CREATE TRIGGER legacy_guard_leagues_delete BEFORE DELETE ON leagues\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'leagues' AND r.pk = CAST(OLD.league_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: leagues row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_matchups_insert",
        "CREATE TRIGGER legacy_guard_matchups_insert BEFORE INSERT ON matchups\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: matchups row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_matchups_update",
        "CREATE TRIGGER legacy_guard_matchups_update BEFORE UPDATE ON matchups\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.matchup_id IS NOT OLD.matchup_id OR NEW.season_id IS NOT OLD.season_id OR NEW.week IS NOT OLD.week OR NEW.team_id IS NOT OLD.team_id OR NEW.opponent_team_id IS NOT OLD.opponent_team_id OR NEW.team_score IS NOT OLD.team_score OR NEW.opponent_score IS NOT OLD.opponent_score OR NEW.is_win IS NOT OLD.is_win OR NEW.is_playoff IS NOT OLD.is_playoff OR NEW.is_consolation IS NOT OLD.is_consolation OR NEW.nfl_com_game_id IS NOT OLD.nfl_com_game_id)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: matchups row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_matchups_delete",
        "CREATE TRIGGER legacy_guard_matchups_delete BEFORE DELETE ON matchups\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: matchups row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_owner_identity_overrides_update",
        "CREATE TRIGGER legacy_guard_owner_identity_overrides_update BEFORE UPDATE ON owner_identity_overrides\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'owner_identity_overrides' AND r.pk = CAST(OLD.override_id AS TEXT)) AND (NEW.override_id IS NOT OLD.override_id OR NEW.league_id IS NOT OLD.league_id OR NEW.external_id_kind IS NOT OLD.external_id_kind OR NEW.external_id_value IS NOT OLD.external_id_value OR NEW.canonical_display_name IS NOT OLD.canonical_display_name OR NEW.notes IS NOT OLD.notes)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: owner_identity_overrides row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_owner_identity_overrides_delete",
        "CREATE TRIGGER legacy_guard_owner_identity_overrides_delete BEFORE DELETE ON owner_identity_overrides\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'owner_identity_overrides' AND r.pk = CAST(OLD.override_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: owner_identity_overrides row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_owners_update",
        "CREATE TRIGGER legacy_guard_owners_update BEFORE UPDATE ON owners\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'owners' AND r.pk = CAST(OLD.owner_id AS TEXT)) AND (NEW.owner_id IS NOT OLD.owner_id OR NEW.league_id IS NOT OLD.league_id OR NEW.display_name IS NOT OLD.display_name OR NEW.nfl_user_id IS NOT OLD.nfl_user_id OR NEW.aliases IS NOT OLD.aliases OR NEW.is_active IS NOT OLD.is_active OR NEW.joined_year IS NOT OLD.joined_year OR NEW.left_year IS NOT OLD.left_year)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: owners row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_owners_delete",
        "CREATE TRIGGER legacy_guard_owners_delete BEFORE DELETE ON owners\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'owners' AND r.pk = CAST(OLD.owner_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: owners row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_availability_insert",
        "CREATE TRIGGER legacy_guard_player_availability_insert BEFORE INSERT ON player_availability\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM seasons s WHERE s.year = NEW.season_year AND s.is_sealed = 1)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_availability row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_availability_update",
        "CREATE TRIGGER legacy_guard_player_availability_update BEFORE UPDATE ON player_availability\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (EXISTS (SELECT 1 FROM seasons s WHERE s.year = OLD.season_year AND s.is_sealed = 1) OR EXISTS (SELECT 1 FROM seasons s WHERE s.year = NEW.season_year AND s.is_sealed = 1)) AND (NEW.availability_id IS NOT OLD.availability_id OR NEW.player_id IS NOT OLD.player_id OR NEW.season_year IS NOT OLD.season_year OR NEW.week IS NOT OLD.week OR NEW.status IS NOT OLD.status OR NEW.owning_team_id IS NOT OLD.owning_team_id OR NEW.waiver_claim_deadline IS NOT OLD.waiver_claim_deadline OR NEW.last_status_change IS NOT OLD.last_status_change OR NEW.is_pre_kickoff_snapshot IS NOT OLD.is_pre_kickoff_snapshot OR NEW.extra_data IS NOT OLD.extra_data)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_availability row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_availability_delete",
        "CREATE TRIGGER legacy_guard_player_availability_delete BEFORE DELETE ON player_availability\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM seasons s WHERE s.year = OLD.season_year AND s.is_sealed = 1)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_availability row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_id_overrides_update",
        "CREATE TRIGGER legacy_guard_player_id_overrides_update BEFORE UPDATE ON player_id_overrides\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'player_id_overrides' AND r.pk = CAST(OLD.override_id AS TEXT)) AND (NEW.override_id IS NOT OLD.override_id OR NEW.external_id_kind IS NOT OLD.external_id_kind OR NEW.external_id_value IS NOT OLD.external_id_value OR NEW.player_id IS NOT OLD.player_id OR NEW.notes IS NOT OLD.notes)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_id_overrides row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_id_overrides_delete",
        "CREATE TRIGGER legacy_guard_player_id_overrides_delete BEFORE DELETE ON player_id_overrides\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'player_id_overrides' AND r.pk = CAST(OLD.override_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_id_overrides row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_identity_links_update",
        "CREATE TRIGGER legacy_guard_player_identity_links_update BEFORE UPDATE ON player_identity_links\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'player_identity_links' AND r.pk = CAST(OLD.link_id AS TEXT)) AND (NEW.link_id IS NOT OLD.link_id OR NEW.member_player_id IS NOT OLD.member_player_id OR NEW.canonical_player_id IS NOT OLD.canonical_player_id OR NEW.source IS NOT OLD.source OR NEW.confidence IS NOT OLD.confidence OR NEW.notes IS NOT OLD.notes)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_identity_links row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_player_identity_links_delete",
        "CREATE TRIGGER legacy_guard_player_identity_links_delete BEFORE DELETE ON player_identity_links\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'player_identity_links' AND r.pk = CAST(OLD.link_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: player_identity_links row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_players_update",
        "CREATE TRIGGER legacy_guard_players_update BEFORE UPDATE ON players\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'players' AND r.pk = CAST(OLD.player_id AS TEXT)) AND (NEW.player_id IS NOT OLD.player_id OR NEW.nfl_com_player_id IS NOT OLD.nfl_com_player_id)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: players row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_players_delete",
        "CREATE TRIGGER legacy_guard_players_delete BEFORE DELETE ON players\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND EXISTS (SELECT 1 FROM legacy_sealed_rows r WHERE r.table_name = 'players' AND r.pk = CAST(OLD.player_id AS TEXT))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: players row is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_scoring_rules_insert",
        "CREATE TRIGGER legacy_guard_scoring_rules_insert BEFORE INSERT ON scoring_rules\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: scoring_rules row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_scoring_rules_update",
        "CREATE TRIGGER legacy_guard_scoring_rules_update BEFORE UPDATE ON scoring_rules\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.rule_id IS NOT OLD.rule_id OR NEW.season_id IS NOT OLD.season_id OR NEW.category IS NOT OLD.category OR NEW.stat_key IS NOT OLD.stat_key OR NEW.points_per_unit IS NOT OLD.points_per_unit OR NEW.unit_size IS NOT OLD.unit_size OR NEW.threshold_min IS NOT OLD.threshold_min OR NEW.threshold_max IS NOT OLD.threshold_max OR NEW.flat_points IS NOT OLD.flat_points OR NEW.raw_text IS NOT OLD.raw_text)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: scoring_rules row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_scoring_rules_delete",
        "CREATE TRIGGER legacy_guard_scoring_rules_delete BEFORE DELETE ON scoring_rules\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: scoring_rules row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_seasons_update",
        "CREATE TRIGGER legacy_guard_seasons_update BEFORE UPDATE ON seasons\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND OLD.is_sealed = 1 AND (NEW.season_id IS NOT OLD.season_id OR NEW.league_id IS NOT OLD.league_id OR NEW.year IS NOT OLD.year OR NEW.status IS NOT OLD.status OR NEW.regular_season_weeks IS NOT OLD.regular_season_weeks OR NEW.playoff_weeks IS NOT OLD.playoff_weeks OR NEW.champion_team_id IS NOT OLD.champion_team_id OR NEW.runner_up_team_id IS NOT OLD.runner_up_team_id OR NEW.last_place_team_id IS NOT OLD.last_place_team_id OR NEW.is_sealed IS NOT OLD.is_sealed OR NEW.sealed_at IS NOT OLD.sealed_at OR NEW.seal_id IS NOT OLD.seal_id)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: season is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_seasons_delete",
        "CREATE TRIGGER legacy_guard_seasons_delete BEFORE DELETE ON seasons\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND OLD.is_sealed = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: season is sealed; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_team_rosters_insert",
        "CREATE TRIGGER legacy_guard_team_rosters_insert BEFORE INSERT ON team_rosters\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT s.is_sealed FROM teams tm JOIN seasons s ON s.season_id = tm.season_id WHERE tm.team_id = NEW.team_id) = 1 OR EXISTS (SELECT 1 FROM seasons s WHERE s.year = NEW.season_year AND s.is_sealed = 1))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: team_rosters row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_team_rosters_update",
        "CREATE TRIGGER legacy_guard_team_rosters_update BEFORE UPDATE ON team_rosters\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (((SELECT s.is_sealed FROM teams tm JOIN seasons s ON s.season_id = tm.season_id WHERE tm.team_id = OLD.team_id) = 1 OR EXISTS (SELECT 1 FROM seasons s WHERE s.year = OLD.season_year AND s.is_sealed = 1)) OR ((SELECT s.is_sealed FROM teams tm JOIN seasons s ON s.season_id = tm.season_id WHERE tm.team_id = NEW.team_id) = 1 OR EXISTS (SELECT 1 FROM seasons s WHERE s.year = NEW.season_year AND s.is_sealed = 1))) AND (NEW.roster_id IS NOT OLD.roster_id OR NEW.team_id IS NOT OLD.team_id OR NEW.player_id IS NOT OLD.player_id OR NEW.season_year IS NOT OLD.season_year OR NEW.week IS NOT OLD.week OR NEW.roster_slot IS NOT OLD.roster_slot OR NEW.is_starter IS NOT OLD.is_starter OR NEW.was_locked_at_kickoff IS NOT OLD.was_locked_at_kickoff OR NEW.acquisition_type IS NOT OLD.acquisition_type OR NEW.acquisition_week IS NOT OLD.acquisition_week OR NEW.acquisition_date IS NOT OLD.acquisition_date OR NEW.drop_date IS NOT OLD.drop_date OR NEW.extra_data IS NOT OLD.extra_data)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: team_rosters row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_team_rosters_delete",
        "CREATE TRIGGER legacy_guard_team_rosters_delete BEFORE DELETE ON team_rosters\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT s.is_sealed FROM teams tm JOIN seasons s ON s.season_id = tm.season_id WHERE tm.team_id = OLD.team_id) = 1 OR EXISTS (SELECT 1 FROM seasons s WHERE s.year = OLD.season_year AND s.is_sealed = 1))\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: team_rosters row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_teams_insert",
        "CREATE TRIGGER legacy_guard_teams_insert BEFORE INSERT ON teams\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: teams row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_teams_update",
        "CREATE TRIGGER legacy_guard_teams_update BEFORE UPDATE ON teams\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.team_id IS NOT OLD.team_id OR NEW.season_id IS NOT OLD.season_id OR NEW.owner_id IS NOT OLD.owner_id OR NEW.team_name IS NOT OLD.team_name OR NEW.team_abbrev IS NOT OLD.team_abbrev OR NEW.draft_position IS NOT OLD.draft_position OR NEW.final_rank IS NOT OLD.final_rank OR NEW.regular_season_wins IS NOT OLD.regular_season_wins OR NEW.regular_season_losses IS NOT OLD.regular_season_losses OR NEW.regular_season_ties IS NOT OLD.regular_season_ties OR NEW.regular_season_points_for IS NOT OLD.regular_season_points_for OR NEW.regular_season_points_against IS NOT OLD.regular_season_points_against OR NEW.made_playoffs IS NOT OLD.made_playoffs OR NEW.playoff_finish IS NOT OLD.playoff_finish OR NEW.team_avatar_asset_id IS NOT OLD.team_avatar_asset_id OR NEW.owner_avatar_asset_id IS NOT OLD.owner_avatar_asset_id)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: teams row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_teams_delete",
        "CREATE TRIGGER legacy_guard_teams_delete BEFORE DELETE ON teams\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: teams row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_transactions_insert",
        "CREATE TRIGGER legacy_guard_transactions_insert BEFORE INSERT ON transactions\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: transactions row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_transactions_update",
        "CREATE TRIGGER legacy_guard_transactions_update BEFORE UPDATE ON transactions\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND ((SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1 OR (SELECT is_sealed FROM seasons WHERE season_id = NEW.season_id) = 1) AND (NEW.transaction_id IS NOT OLD.transaction_id OR NEW.season_id IS NOT OLD.season_id OR NEW.transaction_type IS NOT OLD.transaction_type OR NEW.executed_at IS NOT OLD.executed_at OR NEW.effective_week IS NOT OLD.effective_week OR NEW.team_id IS NOT OLD.team_id OR NEW.counterpart_team_id IS NOT OLD.counterpart_team_id OR NEW.player_id IS NOT OLD.player_id OR NEW.direction IS NOT OLD.direction OR NEW.waiver_priority_used IS NOT OLD.waiver_priority_used OR NEW.notes IS NOT OLD.notes OR NEW.extra_data IS NOT OLD.extra_data)\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: transactions row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_transactions_delete",
        "CREATE TRIGGER legacy_guard_transactions_delete BEFORE DELETE ON transactions\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AND (SELECT is_sealed FROM seasons WHERE season_id = OLD.season_id) = 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: transactions row belongs to a sealed season; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_legacy_amendments_update",
        "CREATE TRIGGER legacy_guard_legacy_amendments_update BEFORE UPDATE ON legacy_amendments\n"
        "WHEN 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_amendments is append-only'); END",
    ),
    (
        "legacy_guard_legacy_amendments_delete",
        "CREATE TRIGGER legacy_guard_legacy_amendments_delete BEFORE DELETE ON legacy_amendments\n"
        "WHEN 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_amendments is append-only'); END",
    ),
    (
        "legacy_guard_legacy_seals_update",
        "CREATE TRIGGER legacy_guard_legacy_seals_update BEFORE UPDATE ON legacy_seals\n"
        "WHEN 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_seals is append-only'); END",
    ),
    (
        "legacy_guard_legacy_seals_delete",
        "CREATE TRIGGER legacy_guard_legacy_seals_delete BEFORE DELETE ON legacy_seals\n"
        "WHEN 1\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_seals is append-only'); END",
    ),
    (
        "legacy_guard_legacy_sealed_rows_insert",
        "CREATE TRIGGER legacy_guard_legacy_sealed_rows_insert BEFORE INSERT ON legacy_sealed_rows\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_sealed_rows changes only inside an unlock; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_legacy_sealed_rows_update",
        "CREATE TRIGGER legacy_guard_legacy_sealed_rows_update BEFORE UPDATE ON legacy_sealed_rows\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_sealed_rows changes only inside an unlock; use ff-pipeline legacy amend'); END",
    ),
    (
        "legacy_guard_legacy_sealed_rows_delete",
        "CREATE TRIGGER legacy_guard_legacy_sealed_rows_delete BEFORE DELETE ON legacy_sealed_rows\n"
        "WHEN COALESCE((SELECT unlocked_until FROM legacy_guard WHERE id = 1), '') < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')\n"
        "BEGIN SELECT RAISE(ABORT, 'legacy_guard: legacy_sealed_rows changes only inside an unlock; use ff-pipeline legacy amend'); END",
    ),
]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "seasons",
        sa.Column("is_sealed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("seasons", sa.Column("sealed_at", sa.Text(), nullable=True))
    op.add_column("seasons", sa.Column("seal_id", sa.Text(), nullable=True))

    op.create_table(
        "legacy_guard",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("unlocked_until", sa.Text(), nullable=True),
        sa.Column("unlocked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_legacy_guard_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO legacy_guard (id, unlocked_until, unlocked_reason) VALUES (1, NULL, NULL)"
    )
    op.create_table(
        "legacy_seals",
        sa.Column("seal_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("alembic_revision", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("seal_id"),
    )
    op.create_table(
        "legacy_sealed_rows",
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("pk", sa.Text(), nullable=False),
        sa.Column("row_hash", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("table_name", "pk"),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "legacy_amendments",
        sa.Column("amendment_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("git_commit", sa.Text(), nullable=True),
        sa.Column("before_seal", sa.Text(), nullable=False),
        sa.Column("after_seal", sa.Text(), nullable=False),
        sa.Column("tables_changed", sa.JSON(), nullable=False),
        sa.Column("rows_changed", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("amendment_id"),
    )

    if op.get_bind().dialect.name == "sqlite":
        for _name, sql in TRIGGERS:
            op.execute(sql)


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "sqlite":
        for name, _sql in reversed(TRIGGERS):
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("legacy_amendments")
    op.drop_table("legacy_sealed_rows")
    op.drop_table("legacy_seals")
    op.drop_table("legacy_guard")
    with op.batch_alter_table("seasons") as batch_op:
        batch_op.drop_column("seal_id")
        batch_op.drop_column("sealed_at")
        batch_op.drop_column("is_sealed")
