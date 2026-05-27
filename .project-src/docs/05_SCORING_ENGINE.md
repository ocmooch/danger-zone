# 05 — Scoring Engine

The scoring engine is the most error-prone piece of the entire system. A subtle bug here propagates everywhere — Phase 2 dashboards show wrong totals, Phase 3 advisors recommend bad trades, your league history reads wrong. This doc is about making it bulletproof.

## Design principles

1. **Scoring rules are data, not code.** They live in the `scoring_rules` table, scraped from your league's settings page. Code reads them; code doesn't hardcode them.
2. **Engine is pure.** `apply_rules(stats: dict, rules: ScoringRules) → ScoredResult`. No I/O, no globals.
3. **Every output is auditable.** The breakdown JSON shows exactly which rule produced which sub-total.
4. **Verification against ground truth is mandatory.** The engine must reproduce NFL.com's stored point totals to within 0.1 points for every historical week we have access to.

## The full set of stat keys we handle

The scoring engine knows about exactly these stat keys, organized by category:

### Passing
- `passing_yards` (per yard)
- `passing_tds` (per TD)
- `passing_interceptions` (per INT, usually negative)
- `passing_2pt_conversions` (per 2pt)
- `passing_yards_bonus_300` (flat bonus at 300+ yards)
- `passing_yards_bonus_400` (flat bonus at 400+ yards)
- `passing_yards_bonus_long_td_40` (flat bonus per TD of 40+ yards)

### Rushing
- `rushing_yards` (per yard)
- `rushing_tds` (per TD)
- `rushing_2pt_conversions` (per 2pt)
- `rushing_yards_bonus_100` (flat at 100+)
- `rushing_yards_bonus_200` (flat at 200+)
- `rushing_yards_bonus_long_td_40` (per long TD)

### Receiving
- `receptions` (per reception — this is the PPR knob; could be 0, 0.5, or 1.0)
- `receiving_yards` (per yard)
- `receiving_tds` (per TD)
- `receiving_2pt_conversions` (per 2pt)
- `receiving_yards_bonus_100` (flat at 100+)
- `receiving_yards_bonus_200` (flat at 200+)
- `receiving_yards_bonus_long_td_40` (per long TD)

### Miscellaneous offensive
- `fumbles_lost` (usually negative)
- `fumble_return_tds` (per fumble recovered for TD)

### Kicking
- `field_goal_made_0_19` (per FG in that distance bracket)
- `field_goal_made_20_29`
- `field_goal_made_30_39`
- `field_goal_made_40_49`
- `field_goal_made_50_plus`
- `extra_point_made` (per PAT)
- `field_goal_missed` (per miss, sometimes negative)
- `extra_point_missed`

### Defense / Special Teams
- `sacks` (per sack)
- `interceptions` (per INT)
- `fumbles_recovered` (per recovered fumble)
- `safeties` (per safety)
- `defensive_tds` (per defensive TD)
- `special_teams_tds` (per ST TD — kickoff/punt returns, blocked kicks)
- `blocked_kicks` (per blocked FG/punt/PAT)
- `points_allowed_0` (flat bonus for shutout)
- `points_allowed_1_6` (flat for 1-6 PA)
- `points_allowed_7_13` (flat for 7-13)
- `points_allowed_14_20` (flat for 14-20)
- `points_allowed_21_27` (flat for 21-27, often 0)
- `points_allowed_28_34` (flat, often negative)
- `points_allowed_35_plus` (flat, often more negative)

This covers virtually every standard and custom rule used in NFL.com leagues. If your league has something exotic (e.g., team-defense yards-allowed brackets), it'll need to be added as a new stat key — but the architecture supports it without schema changes.

## ScoringRules data class

```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ScoringRule:
    category: str            # 'passing', 'rushing', etc.
    stat_key: str            # 'passing_yards', 'passing_tds', etc.
    points_per_unit: float   # e.g., 0.04 (per yard), 6.0 (per TD)
    unit_size: float = 1.0   # e.g., 1.0 for "per yard", 10.0 for "per 10 yards"
    threshold_min: Optional[float] = None  # only applies above this stat value
    threshold_max: Optional[float] = None  # only applies up to this stat value
    flat_points: Optional[float] = None    # for all-or-nothing rules (overrides points_per_unit)

@dataclass(frozen=True)
class ScoringRules:
    """The full set of rules for one season."""
    season_id: int
    rules: tuple[ScoringRule, ...]
```

## Engine logic — pseudocode

```python
def apply_rules(stats: dict[str, float], rules: ScoringRules) -> ScoredResult:
    """
    stats: e.g. {'passing_yards': 312, 'passing_tds': 2, 'rushing_yards': 18, ...}
    rules: the season's ScoringRules
    
    Returns: ScoredResult(total_points=22.48, breakdown={'passing': 18.48, 'rushing': 1.8, 'bonus': 3.0, ...})
    """
    breakdown = defaultdict(float)
    
    for rule in rules.rules:
        stat_value = stats.get(rule.stat_key, 0)
        
        # Bonus / flat-points rule
        if rule.flat_points is not None:
            threshold = rule.threshold_min or 0
            if stat_value >= threshold:
                if rule.threshold_max is None or stat_value <= rule.threshold_max:
                    breakdown[rule.category] += rule.flat_points
            continue
        
        # Threshold-gated per-unit rule (rare but possible)
        effective_value = stat_value
        if rule.threshold_min is not None:
            effective_value = max(0, stat_value - rule.threshold_min)
        if rule.threshold_max is not None:
            effective_value = min(effective_value, rule.threshold_max - (rule.threshold_min or 0))
        
        # Standard per-unit rule
        points = (effective_value / rule.unit_size) * rule.points_per_unit
        breakdown[rule.category] += points
    
    total = round(sum(breakdown.values()), 2)
    return ScoredResult(total_points=total, breakdown=dict(breakdown))
```

The engine is fewer than 30 lines of real logic. The complexity is in the rules data, not the engine code.

## Scraping the rules

NFL.com renders league scoring on a single settings page: `/league/{LID}/settings`. It uses a table per category, with rows that say things like:

- "Passing Yards: 1 point per 25 yards"  → `passing_yards` rule: `points_per_unit=1, unit_size=25`
- "Passing TDs: 4 points"                 → `passing_tds` rule: `points_per_unit=4, unit_size=1`
- "300+ Passing Yards Bonus: 3 points"    → `passing_yards_bonus_300` rule: `flat_points=3, threshold_min=300`

The parser:
1. Locates each scoring category section by its heading text.
2. Iterates rows in the scoring table.
3. Matches the rule label against a **known mapping** (`passing_yards` → `passing_yards` key, `Pass TD` → `passing_tds` key, etc.).
4. Parses the points value (handles "1 / 25 yds", "3 pts at 300", "-2 pts", etc.).
5. Writes one `ScoringRule` row per scoring line.

The mapping table is in `crawlers/nfl_com/scoring_labels.py`. If NFL.com adds a new rule type (rare), the parser logs `Unknown scoring rule: '...'` and the user updates the mapping.

## Verification — how we know the engine is correct

This is the single most important reliability mechanism in Phase 1. Procedure:

1. **Pick three historical weeks** from the user's league where:
   - All players are nflverse-trackable (i.e., not deep waiver-wire kickers)
   - The matchup is fully completed
   - NFL.com still shows the box score
2. **For each player on each starting lineup** in those weeks, run:
   ```
   our_score = scoring_engine.apply_rules(nflverse_stats, scraped_rules)
   nfl_com_score = parse_from_nfl_com_gamecenter_page(...)
   assert abs(our_score - nfl_com_score) < 0.1
   ```
3. **On failure**: log the diff with full breakdown. Iterate.

This is an integration test that runs as part of `pytest tests/integration/test_scoring_verification.py`. **It must pass before the pipeline is considered production-ready.**

## Edge cases the engine must handle

| Case | Handling |
|------|----------|
| Player has no stat line that week (DNP) | Stats dict has all zeros / missing keys; `.get(key, 0)` defaults work; result is 0 points |
| Player is on bye week | Same as DNP — 0 points |
| Stat correction post-game (nflverse re-publishes) | New `player_stats_raw` row with updated `ingested_at`; engine re-runs; new `player_stats_scored` row written; old one retained for audit |
| Defense/ST scoring uses opponent stats | The "stats" dict for a DEF is the OPPONENT'S offensive stats — handled by a different normalizer pre-step that constructs the DEF stat dict from opponent stats |
| Negative stats (INTs, fumbles, missed FGs) | Rule's `points_per_unit` is negative; engine math handles naturally |
| Bonus stacking (300+ AND 400+ yard bonuses) | Each is a separate rule; both trigger independently for a 400-yard game |
| Bonus only above threshold (e.g., long TD 40+ yards) | `threshold_min` on the stat applies; nflverse's `pbp` data provides the per-play yardage we need to compute "TDs of 40+ yards" |

## Where the engine fails on purpose

If we get a stat we don't have a rule for, we **don't silently zero it** — we log a warning so the user knows. This catches: NFL.com adding a new rule type, nflverse adding a new stat field, etc.

```python
unmapped_stats = set(stats.keys()) - {rule.stat_key for rule in rules.rules}
for stat in unmapped_stats:
    log.warning("Unmapped stat in scoring", stat_key=stat, value=stats[stat], player_id=player_id)
```

## "Quick wins" tests the user can sanity-check

After implementation:

```bash
# Spot-check a high-scoring QB week — should match NFL.com to the decimal
ff-pipeline verify --player "Lamar Jackson" --season 2024 --week 12

# Recompute all scoring for a season — should produce zero changes
# if rules haven't changed
ff-pipeline rescore --season 2024 --dry-run
```
