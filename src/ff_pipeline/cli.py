"""Typer-based CLI entry point.

For M0 this provides only `--version`; M2 wires every subcommand from
docs/08_OPERATIONS.md.
"""

from __future__ import annotations

import typer

from ff_pipeline import __version__

app = typer.Typer(
    name="ff-pipeline",
    help="Personal fantasy football data aggregation pipeline (Phase 1).",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """ff-pipeline — see `ff-pipeline --help` for available commands."""


if __name__ == "__main__":
    app()
