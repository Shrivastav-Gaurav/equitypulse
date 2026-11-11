"""`ep` command-line entry point."""

import datetime as dt

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


@app.command()
def backfill(
    from_date: dt.date = typer.Option(  # noqa: B008
        ..., "--from", formats=["%Y-%m-%d"], help="Start date (inclusive)"
    ),
    to_date: dt.date = typer.Option(  # noqa: B008
        ..., "--to", formats=["%Y-%m-%d"], help="End date (inclusive)"
    ),
    universe: str = typer.Option(  # noqa: B008
        None, "--universe", help="Only load current members of this index, e.g. NIFTY500"
    ),
) -> None:
    """Download and store daily bhavcopy for a date range.

    Skips non-trading days and dates already loaded; never uses the Tapetide fallback;
    stops if NSE refuses three dates in a row. One audit run is written per date.
    Run the calendar, equity master and (for --universe) index membership first.
    """
    from equitypulse.calendar import NseCalendar
    from equitypulse.config import get_settings
    from equitypulse.db.session import session_factory
    from equitypulse.index_load import open_members
    from equitypulse.jobs.backfill import run_backfill

    delay = get_settings().nse_request_delay_seconds

    with session_factory()() as session:
        try:
            cal = NseCalendar.from_session(session)
        except Exception:
            typer.echo("Calendar not loaded: refresh the trading calendar first.", err=True)
            raise typer.Exit(1) from None

        members = None
        if universe:
            members = open_members(session, universe)
            if not members:
                typer.echo(f"No members loaded for index {universe}.", err=True)
                raise typer.Exit(1)
            typer.echo(f"Universe {universe}: {len(members)} ISINs")

        result = run_backfill(
            session, cal, from_date, to_date, universe=members, delay_seconds=delay,
            progress=lambda msg: typer.echo(f"  {msg}"),
        )

    typer.echo(
        f"Backfill: loaded {result.loaded} dates ({result.rows_written} rows), "
        f"already loaded {result.already_loaded}, failed {len(result.failed)}"
    )
    if result.blocked:
        typer.echo("STOPPED: NSE refused repeated downloads; it may be blocking us.", err=True)
        raise typer.Exit(2)
    if result.failed:
        raise typer.Exit(1)


@app.command()
def nightly(
    date: dt.date = typer.Option(  # noqa: B008
        None, "--date", formats=["%Y-%m-%d"], help="Market date to run for (default: today IST)"
    ),
) -> None:
    """Run nightly ingest: calendar check → master → bhavcopy → surveillance."""
    from equitypulse.db.session import session_factory
    from equitypulse.jobs.nightly import run_nightly

    with session_factory()() as session:
        result = run_nightly(session, date=date)
        session.commit()

    if result.skipped:
        typer.echo(f"Skipped {result.date}: not a trading day.")
        return
    typer.echo(f"Nightly {result.date}: master +{result.master_rows}, "
               f"prices +{result.bhavcopy_rows}, surveillance +{result.surveillance_rows}"
               + (" [DEGRADED]" if result.degraded else ""))


@app.command()
def status() -> None:
    """Show freshness per source and Tapetide calls used today."""
    from equitypulse.audit import status_summary
    from equitypulse.db.session import session_factory

    with session_factory()() as session:
        rows = status_summary(session)

    if not rows:
        typer.echo("No runs recorded yet.")
        return

    typer.echo(f"{'source':<32}  {'last_ok':<24}  {'rows':>10}")
    typer.echo("-" * 72)
    for row in rows:
        if row.get("calls") is not None:
            typer.echo(f"  Tapetide calls today: {row['calls']}")
        else:
            last = str(row.get("last_ok") or "never")[:24]
            rows_str = str(row.get("total_rows") or 0)
            typer.echo(f"{row['source']:<32}  {last:<24}  {rows_str:>10}")
