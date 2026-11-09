"""``ff-pipeline legacy`` sub-commands and the preflight used by writing commands."""

from __future__ import annotations

# typer reads parameter annotations at runtime, so Path must be a real import.
from pathlib import Path

import typer

legacy_app = typer.Typer(
    name="legacy",
    help="Legacy guard for the sealed NFL.com era (2010-2025): seal, verify, status, amend.",
    no_args_is_help=True,
)

DEFAULT_MANIFEST = Path("docs/sleeper/seals/seal-v1.manifest.json")


def _db_path() -> str:
    """The SQLite file behind DATABASE_URL (the guard is SQLite-only)."""
    from ff_pipeline.observability import sqlite_path_from_url
    from ff_pipeline.settings import get_settings

    path = sqlite_path_from_url(get_settings().database_url)
    if path is None:
        typer.secho("The legacy guard only supports SQLite databases.", fg="red", err=True)
        raise typer.Exit(code=2)
    if not path.exists():
        typer.secho(f"Database file does not exist: {path}", fg="red", err=True)
        raise typer.Exit(code=2)
    return str(path)


def _bootstrap() -> None:
    from ff_pipeline.cli import _bootstrap_settings_and_logging

    _bootstrap_settings_and_logging()


def _parse_years(spec: str) -> list[int]:
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            years.update(range(int(lo), int(hi) + 1))
        else:
            years.add(int(part))
    return sorted(years)


def _year_ranges(years: list[int]) -> str:
    if not years:
        return "none"
    out: list[str] = []
    start = prev = years[0]
    for y in [*years[1:], None]:
        if y is not None and y == prev + 1:
            prev = y
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if y is not None:
            start = prev = y
    return ", ".join(out)


def run_preflight(command_name: str) -> None:
    """Abort a writing command if the sealed data no longer matches its seal.

    A no-op when the DB is not SQLite, does not exist yet, or has no seal
    adopted (fresh and test databases).
    """
    from ff_pipeline.legacy.verify import connect, verify
    from ff_pipeline.observability import sqlite_path_from_url
    from ff_pipeline.settings import get_settings

    path = sqlite_path_from_url(get_settings().database_url)
    if path is None or not path.exists():
        return
    conn = connect(str(path))
    try:
        report = verify(conn, full=False)
    finally:
        conn.close()
    if report.ok:
        return
    typer.secho(f"legacy preflight failed; `{command_name}` was not run.", fg="red", err=True)
    for line in report.lines():
        typer.secho(line, err=True)
    raise typer.Exit(code=1)


@legacy_app.command("seal")
def seal_cmd(
    adopt_manifest: Path = typer.Option(  # noqa: B008  (typer-idiomatic)
        DEFAULT_MANIFEST,
        "--adopt",
        help="Committed seal manifest to adopt (the live DB must match it exactly).",
    ),
    years: str = typer.Option(
        "2010-2025", "--years", help="Seasons to seal, e.g. 2010-2025 or 2010,2012."
    ),
) -> None:
    """One-time: verify the live DB against a seal manifest, store the baseline, seal seasons."""
    _bootstrap()
    from ff_pipeline.legacy.seal import LegacyError, adopt
    from ff_pipeline.legacy.verify import connect, verify

    if not adopt_manifest.is_file():
        typer.secho(f"Manifest not found: {adopt_manifest}", fg="red", err=True)
        raise typer.Exit(code=2)
    conn = connect(_db_path())
    try:
        result = adopt(conn, adopt_manifest, _parse_years(years))
        report = verify(conn, full=False)
    except LegacyError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(
        f"Adopted {result.seal_id}: sealed {_year_ranges(result.years)}; "
        f"{result.sealed_rows} rows registered."
    )
    for line in report.lines():
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(code=1)


@legacy_app.command("verify")
def verify_cmd(
    quick: bool = typer.Option(
        False, "--quick", help="Triggers, sealed seasons, row counts and table hashes only."
    ),
) -> None:
    """Compare the live DB with the current seal. Never repairs; exit 1 on mismatch."""
    _bootstrap()
    from ff_pipeline.legacy.verify import connect, verify

    conn = connect(_db_path())
    try:
        report = verify(conn, full=not quick)
    finally:
        conn.close()
    for line in report.lines():
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(code=1)


@legacy_app.command("status")
def status_cmd() -> None:
    """Seal id and date, sealed seasons, lock state, last amendment."""
    _bootstrap()
    from ff_pipeline.legacy.fingerprint import sealed_years
    from ff_pipeline.legacy.seal import now_text
    from ff_pipeline.legacy.triggers import trigger_names
    from ff_pipeline.legacy.verify import connect, current_seal, has_table

    conn = connect(_db_path())
    try:
        if not has_table(conn, "legacy_guard"):
            typer.echo("Legacy guard not installed (run `ff-pipeline migrate up`).")
            return
        seal = current_seal(conn)
        if seal is None:
            typer.echo("Seal: none adopted (guards are inert).")
        else:
            typer.echo(f"Seal: {seal.seal_id} ({seal.kind}, {seal.created_at})")
        typer.echo(f"Sealed seasons: {_year_ranges(sealed_years(conn))}")
        until, why = conn.execute(
            "SELECT unlocked_until, unlocked_reason FROM legacy_guard WHERE id = 1"
        ).fetchone() or (None, None)
        if until is not None and until > now_text():
            typer.secho(f"Lock: UNLOCKED until {until} ({why})", fg="yellow")
        else:
            typer.echo("Lock: locked")
        live = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        expected = trigger_names()
        typer.echo(f"Guard triggers: {sum(n in live for n in expected)}/{len(expected)}")
        last = conn.execute(
            "SELECT amendment_id, created_at, reason, after_seal FROM legacy_amendments"
            " ORDER BY amendment_id DESC LIMIT 1"
        ).fetchone()
        if last is None:
            typer.echo("Last amendment: none")
        else:
            typer.echo(f"Last amendment: #{last[0]} {last[1]} -> {last[3]}: {last[2]}")
    finally:
        conn.close()


@legacy_app.command(
    "amend",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def amend_cmd(
    ctx: typer.Context,
    reason: str = typer.Option(..., "--reason", help="Why sealed data must change (audited)."),
    unlock_minutes: float = typer.Option(
        10.0, "--unlock-minutes", min=1.0, max=60.0, help="Length of the unlock window."
    ),
    backup_dir: Path | None = typer.Option(  # noqa: B008  (typer-idiomatic)
        None,
        "--backup-dir",
        help="Where the pre-amend backup goes (default: data/backups/amendments, never pruned).",
    ),
) -> None:
    """Run a command with sealed data unlocked, then audit and re-seal what it changed.

    Usage: ff-pipeline legacy amend --reason "…" -- <command …>
    """
    _bootstrap()
    from ff_pipeline.legacy.amend import amend
    from ff_pipeline.legacy.seal import LegacyError
    from ff_pipeline.legacy.verify import connect
    from ff_pipeline.settings import PROJECT_ROOT

    command = list(ctx.args)
    target_dir = (backup_dir or PROJECT_ROOT / "data" / "backups" / "amendments").resolve()
    conn = connect(_db_path())
    try:
        result = amend(
            conn,
            reason=reason,
            command=command,
            backup_dir=target_dir,
            unlock_minutes=unlock_minutes,
        )
    except LegacyError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Pre-amend backup: {result.backup_path}")
    typer.echo(f"Command exit code: {result.returncode}; guard relocked.")
    if result.amendment_id is None:
        typer.echo(f"No sealed data changed; {result.before_seal} stays current.")
    else:
        typer.echo(f"Amendment #{result.amendment_id}: {result.before_seal} -> {result.after_seal}")
        for diff in result.diffs:
            typer.echo(f"  {diff.describe()}")
        typer.echo(
            "Follow-up (S9 automates this): build and sync a vault archive for "
            f"{result.after_seal} with scripts/vault/build_vault_dir.py and "
            "scripts/vault/vault_sync.sh."
        )
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode if 0 < result.returncode < 256 else 1)


@legacy_app.command("quarantine-2026-stub")
def quarantine_cmd(
    sleeper_league_id: str | None = typer.Option(
        None, "--sleeper-league-id", help="Default: SLEEPER_LEAGUE_ID (the 2026 league)."
    ),
    backup_dir: Path | None = typer.Option(  # noqa: B008  (typer-idiomatic)
        None,
        "--backup-dir",
        help="Where the pre-move backup goes (default: data/backups/amendments, never pruned).",
    ),
) -> None:
    """Move the NFL.com 2026 placeholder into its quarantine table (D-2; S4), audited."""
    _bootstrap()
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from ff_pipeline.legacy.amend import backup_database
    from ff_pipeline.legacy.quarantine import quarantine_2026_stub
    from ff_pipeline.legacy.seal import LegacyError
    from ff_pipeline.legacy.verify import connect
    from ff_pipeline.repository.database import create_app_engine
    from ff_pipeline.repository.maintenance import recompute_rostered_spans
    from ff_pipeline.settings import PROJECT_ROOT, SettingsError, get_settings

    settings = get_settings()
    try:
        league_id = sleeper_league_id or settings.require_sleeper_league_id()
    except SettingsError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=4) from exc
    target_dir = (backup_dir or PROJECT_ROOT / "data" / "backups" / "amendments").resolve()
    conn = connect(_db_path())
    try:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target_dir / f"fantasy-pre-quarantine-2026-stub-{stamp}.db"
        backup_database(conn, backup)
        typer.echo(f"Pre-move backup: {backup}")
        result = quarantine_2026_stub(conn, sleeper_league_id=league_id)
    except LegacyError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    if result.already_done:
        typer.echo("The 2026 placeholder is already quarantined; nothing to do.")
        return
    engine = create_app_engine(settings.database_url)
    try:
        with Session(engine) as ss:
            spans = recompute_rostered_spans(ss)
            ss.commit()
    finally:
        engine.dispose()
    typer.echo(
        f"Amendment #{result.amendment_id}: moved {len(result.team_ids)} teams + "
        f"{result.roster_rows} roster rows of season {result.season_id} into "
        f"legacy_nfl_com_2026_preseason; season 2026 is now platform 'sleeper' "
        f"({league_id}). Rostered spans refreshed ({spans} players). "
        f"Sealed data unchanged: {result.seal_id} stays current."
    )
