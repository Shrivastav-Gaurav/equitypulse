"""`ep` command-line entry point.

Later tasks add `status`, `backfill` and `nightly` as subcommands on this app.
"""

import typer

from equitypulse import __version__

app = typer.Typer(
    name="ep",
    help="EquityPulse: personal Indian-equity trade planner.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"equitypulse {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """EquityPulse: personal Indian-equity trade planner."""
