# docs/archive — completed plan documents

These are **finished** planning documents, kept for provenance. Each drove a
specific piece of Phase 1 work that is now built, tested, merged, and shipped
(Phase 1 was released as v1.0.0 on 2026-05-29). They are not maintained and may
reference branches or intermediate states that no longer exist.

The canonical, maintained documentation lives in the numbered docs under
`docs/` (`01_SPEC` … `10_OPEN_QUESTIONS`). Start there. Use these only to
understand *how* a past decision was reached.

| Document | Closed | What it planned |
| --- | --- | --- |
| `PHASE1_COMPLETION_PLAN.md` | 2026-05-29 | Closed the three Phase 1 exit criteria (historical reconstruction, identity completeness, API error contract). Shipped as v1.0.0. |
| `PHASE_LEAGUE_HISTORY_PLAN.md` | 2026-06-05 | League-history media + event-log backfill (team avatars, assets store); run and validated against live NFL.com. |
| `PHASE_PRE2016_PLAN.md` | 2026-06-05 | Distilled 2010–2015 scoring rules and era nuances from labeled data (closed §P1-V1). The one remaining follow-up (the `_CONFIRMED` switch) is downstream **dz-dashboard** work, not this repo. |

Still-live companions that stayed in `docs/` (not archived):
- `PRE2016_STRUCTURAL_REFERENCE.md` — a data-distilled reference (roster
  templates, season-length switch, division map) consumed by dz-dashboard.
- `10_OPEN_QUESTIONS_ARCHIVE.md` — resolved open questions.
