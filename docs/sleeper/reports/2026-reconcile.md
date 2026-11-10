# 2026 reconcile: reconstructed vs Sleeper official points

Generated 2026-11-09. Tolerance 0.01 per player-week. Finalized weeks: 1-8.

- **Official**: Sleeper's per-player points (`team_rosters.extra_data.platform_points`).
- **Reconstructed**: our engine over nflverse stats with the D-10 rules.
- **Sleeper-stats score**: our engine over Sleeper's own stat line (S6-D3).

## Summary (finalized weeks)

| Class | Started | All rostered |
|---|---:|---:|
| match (within tolerance) | 863 | 1550 |
| stat source (Sleeper's own stats reproduce official; nflverse differs) | 1 | 1 |
| rule (Sleeper's own stats do not reproduce official) | 0 | 0 |
| unexplained (no Sleeper stats to compare) | 0 | 0 |
| **total** | 864 | 1551 |

Team totals (sum of reconstructed starters vs Sleeper's matchup score, tolerance 0.01): **95 / 96** match.

## Stat-source divergences by stat (all rostered)

| Stat key | Player-weeks |
|---|---:|
| `fumbles_lost` | 1 |

## Divergences (finalized weeks, all rostered)

| Wk | Player | Pos | Started | Official | Reconstructed | Δ | Class | nflverse → Sleeper |
|---:|---|---|:-:|---:|---:|---:|---|---|
| 8 | Jalen Coker | WR | ✓ | 9.6 | 11.6 | +2 | stat_source | fumbles_lost 0→1 |

## Provisional (weeks not finalized by Sleeper)

match: 195, stat_source: 3 (all rostered). Stats are still moving; not counted above.

## Notes

- **The one residual** (week 8, a lateral receiver's lost fumble): Sleeper charges the fumble to
  the player who received the lateral; nflverse's weekly stats don't count a lateral receiver's
  fumble as his. One play; left as a documented stat-source difference, not special-cased.
- **Before the fixes** in this build the same run showed 98 divergences: 96 were 26 rookies whose
  stats sat on a duplicate player row (fixed by `sleeper-league adopt-gsis`), 2 were a
  catch-and-lateral long TD credited to the catcher (fixed in `long_td_bonus.py`).
- **DST definitions** were checked stat by stat against Sleeper's DEF lines (weeks 1-8, 242
  team-weeks): points allowed 242/242 with the Sleeper convention (the conversion after a
  defensive/return TD is charged), yards allowed 242/242, sacks 241/242 (one team-week's count
  differs between the feeds; that DEF was not rostered).
- Generated on a copy of the database; the live database gets the same run at the 2026 go-live.
