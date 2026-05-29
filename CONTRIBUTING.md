# Contributing

This is a single-author personal project. The conventions below exist so future-me (and any Claude Code session that joins this repo) can pick the work up cold.

## Branch model

```
main              — production; stable; protected
  └── dev         — integration/staging; base for all branches; protected
        ├── feature/<name>   — new work; cut from dev; PR back to dev
        └── hotfix/<name>    — urgent prod fixes; cut from main; PR to main + cherry-pick to dev
```

- `feature/*` PRs go to **dev**, never to `main`.
- `hotfix/*` PRs go to **main**; sync to `dev` afterwards via a separate PR.
- `dev → main` is a deliberate promotion step, never implicit. Tag `main` on each release.
- Delete merged short-lived branches (local + remote) as soon as the PR confirms a clean merge. Never delete `main` or `dev`.
- Never commit directly to `main` or `dev`.

## Commits

Every commit must communicate **what** changed and **why**. AI-assisted commits also carry trailers.

```
<imperative subject — 1 line, present tense>

<body — 2-3 sentences on context / motivation>

AI-Model: <exact model id, e.g. claude-opus-4-7>
Prompted-By: ocmooch <https://github.com/ocmooch>
Reviewed-By: ocmooch <https://github.com/ocmooch>
```

- Subject is imperative ("Add rate limiting", not "Added rate limiting").
- Body explains the *why*. The diff already shows the *what*.
- The three AI trailers (`AI-Model`, `Prompted-By`, `Reviewed-By`) are the only attribution. **Do not** use `Co-Authored-By: Claude` or any Anthropic line.
- Mixed human/AI commits use the same trailers — no extra annotation needed.
- Pass the message through a HEREDOC so formatting is preserved:
  ```bash
  git commit -m "$(cat <<'EOF'
  M11: documentation pass

  Brings README and 08_OPERATIONS in line with the shipped CLI surface,
  introduces RUNBOOK + CONTRIBUTING, and tightens pyproject metadata.

  AI-Model: claude-opus-4-7
  Prompted-By: ocmooch <https://github.com/ocmooch>
  Reviewed-By: ocmooch <https://github.com/ocmooch>
  EOF
  )"
  ```

## Development workflow

```bash
uv sync                          # install / refresh deps
uv run pre-commit install        # one-time hook setup
uv run ff-pipeline --help        # smoke test the CLI

# fast feedback loop
uv run pytest tests/unit -x      # unit tests, stop on first fail
uv run ruff check src tests      # lint
uv run ruff format src tests     # apply formatter
uv run mypy src                  # type-check (strict mode)

# full test suite (integration + unit)
uv run pytest
```

Before opening a PR, all of the following must be green:

- `uv run pytest`
- `uv run ruff check src tests`
- `uv run ruff format --check src tests`
- `uv run mypy src`

## Code conventions

- **Type hints everywhere.** `mypy --strict` is the bar. New modules opt in by default; no `Any` without a comment explaining why.
- **No silent stubs.** If a CLI subcommand isn't implemented yet, it exits 64 with a `[stub]` line so cron output flags it.
- **Idempotent writes.** Every DB write goes through `repository/upsert.py` or matches its `ON CONFLICT` semantics. Re-running any sync must converge to the same state.
- **Settings only via `get_settings()`.** Never read env vars directly inside business logic — settings are validated once at CLI boundary.
- **Comments explain *why*, not *what*.** Well-named identifiers cover the *what*. Add a comment only when the constraint, invariant, or workaround would surprise a future reader.

## Tests

The layers track [`docs/07_TESTING_STRATEGY.md`](docs/07_TESTING_STRATEGY.md):

- `tests/unit/` — pure functions, no I/O. Scoring engine, parsers (against fixtures), upserts (against in-memory SQLite).
- `tests/integration/` — wired through `Session`, alembic round-trips, API TestClient.
- `tests/fixtures/` — saved HTML / parquet / CSV samples. Treat these as test inputs, not generated artifacts; commit them.

A parser fix without a new fixture is incomplete. A scoring fix without a new unit test is incomplete.

## Documentation

- Roadmap state lives in [`docs/09_ROADMAP.md`](docs/09_ROADMAP.md) — update the per-milestone "Done when" checkboxes as work lands.
- Day-2 operational changes go in [`docs/08_OPERATIONS.md`](docs/08_OPERATIONS.md) and [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
- API changes go in [`docs/06_API_CONTRACT.md`](docs/06_API_CONTRACT.md) (canonical — Phase 2/3 read this).
- Open questions / deferred decisions belong in [`docs/10_OPEN_QUESTIONS.md`](docs/10_OPEN_QUESTIONS.md), not in CHANGELOG-style files.

## Running the API

```bash
uv run ff-pipeline serve              # binds 127.0.0.1:8000
uv run ff-pipeline serve --reload     # dev mode, auto-reloads on edits
```

Then [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for the Swagger console. Click every endpoint you touched before declaring a PR done.

## License & attribution

Personal-use project; no formal license. NFL stat data is CC-BY 4.0 via nflverse — preserve their attribution if you republish anything derived. NFL.com data is fetched under personal-account credentials only; do not commit cookies, exports, or scraped HTML with personally identifiable team / league names beyond what's already in fixtures.
