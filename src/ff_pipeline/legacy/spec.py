"""What the legacy guard protects.

The legacy column lists are frozen copies of the seal-v1 manifest
(``docs/sleeper/seals/seal-v1.manifest.json``); a unit test keeps them equal.
They are constants rather than read from the manifest at runtime so the
installed package and the frozen migration never depend on a docs path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GuardKind(StrEnum):
    """How a table's rows are tied to the seal."""

    SEASON = "season"  # rows belong to a season; sealed when that season is
    SEASON_ROW = "season_row"  # the ``seasons`` table itself
    REGISTERED = "registered"  # global rows; sealed when listed in legacy_sealed_rows


@dataclass(frozen=True)
class GuardedTable:
    name: str
    kind: GuardKind
    pk: str
    legacy_columns: tuple[str, ...]
    # How a SEASON row finds its season: "season_id", "season_year" or
    # "team_id" (through teams.season_id). None for the other kinds.
    season_link: str | None = None
    # SQL predicate (over alias ``t``) choosing which rows get sealed at adopt
    # time. Only used for REGISTERED tables; None means every row.
    adopt_filter: str | None = None


SEAL_V1_ID = "seal-v1"
SEAL_V1_YEARS: tuple[int, ...] = tuple(range(2010, 2026))
# Extra columns of the seal row itself that only an unlock may change, so a
# season cannot be un-sealed by a plain UPDATE.
SEASON_SEAL_COLUMNS: tuple[str, ...] = ("is_sealed", "sealed_at", "seal_id")


GUARDED_TABLES: tuple[GuardedTable, ...] = (
    GuardedTable(
        "commissioners",
        GuardKind.REGISTERED,
        "commissioner_id",
        ("commissioner_id", "league_id", "owner_id", "from_year", "to_year", "notes"),
    ),
    GuardedTable(
        "leagues",
        GuardKind.REGISTERED,
        "league_id",
        ("league_id", "name", "platform", "current_season_year"),
    ),
    GuardedTable(
        "matchups",
        GuardKind.SEASON,
        "matchup_id",
        (
            "matchup_id",
            "season_id",
            "week",
            "team_id",
            "opponent_team_id",
            "team_score",
            "opponent_score",
            "is_win",
            "is_playoff",
            "is_consolation",
            "nfl_com_game_id",
        ),
        season_link="season_id",
    ),
    GuardedTable(
        "owner_identity_overrides",
        GuardKind.REGISTERED,
        "override_id",
        (
            "override_id",
            "league_id",
            "external_id_kind",
            "external_id_value",
            "canonical_display_name",
            "notes",
        ),
    ),
    GuardedTable(
        "owners",
        GuardKind.REGISTERED,
        "owner_id",
        (
            "owner_id",
            "league_id",
            "display_name",
            "nfl_user_id",
            "aliases",
            "is_active",
            "joined_year",
            "left_year",
        ),
    ),
    GuardedTable(
        "player_availability",
        GuardKind.SEASON,
        "availability_id",
        (
            "availability_id",
            "player_id",
            "season_year",
            "week",
            "status",
            "owning_team_id",
            "waiver_claim_deadline",
            "last_status_change",
            "is_pre_kickoff_snapshot",
            "extra_data",
        ),
        season_link="season_year",
    ),
    GuardedTable(
        "player_id_overrides",
        GuardKind.REGISTERED,
        "override_id",
        ("override_id", "external_id_kind", "external_id_value", "player_id", "notes"),
    ),
    GuardedTable(
        "player_identity_links",
        GuardKind.REGISTERED,
        "link_id",
        ("link_id", "member_player_id", "canonical_player_id", "source", "confidence", "notes"),
    ),
    GuardedTable(
        "players",
        GuardKind.REGISTERED,
        "player_id",
        ("player_id", "nfl_com_player_id"),
        adopt_filter="t.nfl_com_player_id IS NOT NULL",
    ),
    GuardedTable(
        "scoring_rules",
        GuardKind.SEASON,
        "rule_id",
        (
            "rule_id",
            "season_id",
            "category",
            "stat_key",
            "points_per_unit",
            "unit_size",
            "threshold_min",
            "threshold_max",
            "flat_points",
            "raw_text",
        ),
        season_link="season_id",
    ),
    GuardedTable(
        "seasons",
        GuardKind.SEASON_ROW,
        "season_id",
        (
            "season_id",
            "league_id",
            "year",
            "status",
            "regular_season_weeks",
            "playoff_weeks",
            "champion_team_id",
            "runner_up_team_id",
            "last_place_team_id",
        ),
    ),
    GuardedTable(
        "team_rosters",
        GuardKind.SEASON,
        "roster_id",
        (
            "roster_id",
            "team_id",
            "player_id",
            "season_year",
            "week",
            "roster_slot",
            "is_starter",
            "was_locked_at_kickoff",
            "acquisition_type",
            "acquisition_week",
            "acquisition_date",
            "drop_date",
            "extra_data",
        ),
        season_link="team_id",
    ),
    GuardedTable(
        "teams",
        GuardKind.SEASON,
        "team_id",
        (
            "team_id",
            "season_id",
            "owner_id",
            "team_name",
            "team_abbrev",
            "draft_position",
            "final_rank",
            "regular_season_wins",
            "regular_season_losses",
            "regular_season_ties",
            "regular_season_points_for",
            "regular_season_points_against",
            "made_playoffs",
            "playoff_finish",
            "team_avatar_asset_id",
            "owner_avatar_asset_id",
        ),
        season_link="season_id",
    ),
    GuardedTable(
        "transactions",
        GuardKind.SEASON,
        "transaction_id",
        (
            "transaction_id",
            "season_id",
            "transaction_type",
            "executed_at",
            "effective_week",
            "team_id",
            "counterpart_team_id",
            "player_id",
            "direction",
            "waiver_priority_used",
            "notes",
            "extra_data",
        ),
        season_link="season_id",
    ),
)

GUARDED_BY_NAME: dict[str, GuardedTable] = {t.name: t for t in GUARDED_TABLES}


# ── guard v2: Sleeper-era coverage (S9-D6) ───────────────────────────────────
#
# Columns added by the S3 platform-era schema, guarded for sealed seasons on top
# of the frozen seal-v1 lists above (which stay byte-identical), plus
# ``playoff_brackets`` as a season-scoped table. Re-derivable tables
# (player_stats_*, player_status_snapshots, player_injury_reports,
# source_payloads) and ``traded_picks`` (future picks) are left out on purpose.
# A NFL.com season carries no values in these columns, so sealing behaviour for
# 2010-2025 does not change.

GUARD_V2_TABLES: tuple[GuardedTable, ...] = (
    GuardedTable(
        "matchups",
        GuardKind.SEASON,
        "matchup_id",
        ("platform_matchup_key", "extra_data"),
        season_link="season_id",
    ),
    GuardedTable(
        "playoff_brackets",
        GuardKind.SEASON,
        "bracket_id",
        (
            "bracket_id",
            "season_id",
            "bracket",
            "round",
            "match_no",
            "team1_id",
            "team2_id",
            "winner_team_id",
            "loser_team_id",
            "placement",
            "team1_from",
            "team2_from",
            "source",
        ),
        season_link="season_id",
    ),
    GuardedTable(
        "seasons",
        GuardKind.SEASON_ROW,
        "season_id",
        ("platform", "platform_league_id", "platform_draft_id", "settings_snapshot"),
    ),
    GuardedTable(
        "teams",
        GuardKind.SEASON,
        "team_id",
        ("platform_team_key", "division", "extra_data"),
        season_link="season_id",
    ),
    GuardedTable(
        "transactions",
        GuardKind.SEASON,
        "transaction_id",
        ("platform_transaction_id", "status"),
        season_link="season_id",
    ),
)
# Tables guard v2 covers completely (insert / update / delete), not just columns.
GUARD_V2_FULL_TABLES = frozenset({"playoff_brackets"})
