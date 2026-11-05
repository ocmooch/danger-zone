# Sleeper Era — planning set

The league moved from NFL.com (defunct) to Sleeper for 2026. These documents plan the move across
both repos. Status: **planned, not started.**

| Doc | What it covers |
|-----|----------------|
| [00_SLEEPER_ERA_MASTER_PLAN.md](00_SLEEPER_ERA_MASTER_PLAN.md) | Program plan for both repos: architecture, contracts, capability matrix, joint phasing, decisions, risks |
| [01_PIPELINE_ARCHITECTURE.md](01_PIPELINE_ARCHITECTURE.md) | ff-pipeline design: Sleeper league adapter, raw archive, schema additions, identity, scoring |
| [02_PIPELINE_BUILD_PLAN.md](02_PIPELINE_BUILD_PLAN.md) | Milestones S0–S9 |
| [03_LEGACY_PRESERVATION.md](03_LEGACY_PRESERVATION.md) | **Start here.** Sealing and protecting the NFL.com era (S0/S1) |
| [04_SLEEPER_FIELD_MAPPING.md](04_SLEEPER_FIELD_MAPPING.md) | Sleeper endpoint/field → canonical schema crosswalk (confirm ⚑ items in S2) |

Dashboard side: `../../../dz-dashboard/docs/sleeper/` (architecture, build plan D0–D8, cross-platform UX rules).

Planned subfolders: `seals/` (seal manifests — hashes only), `reports/` (scoring reconcile reports).
