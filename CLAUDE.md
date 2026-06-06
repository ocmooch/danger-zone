# CLAUDE.md - danger-zone

Read this first. This repo is Claude Code canonical; other agent entrypoints should
delegate here instead of duplicating policy.

## What this is

`danger-zone` is Phase 1 of the fantasy football project: the `ff-pipeline` data
foundation. It ingests NFL.com, nflverse, and Sleeper data; normalizes player identity;
scores league results; writes the SQLite database; and exposes a read API for downstream
dashboard and decision-support tools.

Phase 2 (`dz-dashboard`) depends on this repo's package and database, so schema and API
changes here can affect downstream consumers.

## Hard rules

- Never commit secrets, cookies, live exports, or personal credentials.
- Preserve idempotent writes. DB writes go through `repository/upsert.py` or equivalent
  `ON CONFLICT` semantics so reruns converge.
- Parser fixes require saved fixtures in `tests/fixtures/`.
- Scoring fixes require focused unit tests.
- Settings are loaded through `get_settings()` at the boundary; do not read environment
  variables directly in business logic.
- Keep strict typing. New code should pass `mypy --strict` under the repo config.
- Do not modify `uv.lock` unless dependency resolution is explicitly in scope.
- Keep changes scoped to the current milestone, fix, or operational issue.
- Commit trailers: `AI-Model` / `Prompted-By` / `Reviewed-By`. Never use
  `Co-Authored-By: Claude`.

## Source of truth

- `CONTRIBUTING.md` - branch model, commit format, development workflow, code
  conventions, and test expectations.
- `docs/09_ROADMAP.md` - milestone state and "Done when" criteria.
- `docs/02_ARCHITECTURE.md` - module layout and system boundaries.
- `docs/04_DATA_MODEL.md` - database schema.
- `docs/05_SCORING_ENGINE.md` - scoring behavior.
- `docs/06_API_CONTRACT.md` - API contract consumed by later phases.
- `docs/07_TESTING_STRATEGY.md` - test layers.
- `docs/08_OPERATIONS.md` and `docs/RUNBOOK.md` - day-2 operations.
- `docs/10_OPEN_QUESTIONS.md` - unresolved decisions and deferred work.

Read docs by the section needed for the task. Grep for symbols before opening large spans.

## Commands

Fast loop:
```
uv run pytest tests/unit -x
uv run ruff check src tests
uv run mypy src
```

Full gate before a PR:
```
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Operational smoke:
```
uv run ff-pipeline --help
uv run ff-pipeline status --verbose
```

## Done when

- The relevant roadmap or issue criteria are satisfied.
- Tests cover the behavior changed.
- The full gate is green before PR.
- Docs are updated when behavior, operations, API shape, or milestone state changes.
- The commit follows the trailer format in `CONTRIBUTING.md`.
