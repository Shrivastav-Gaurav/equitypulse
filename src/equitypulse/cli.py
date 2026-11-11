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
) -> None:
    """Download and store daily bhavcopy for a date range.

    Skips non-trading days. Rate-limited to avoid hammering NSE.
    Run the equity master first so ISINs resolve cleanly.
    """
    import time

    from equitypulse.calendar import NseCalendar
    from equitypulse.config import get_settings
    from equitypulse.db.session import session_factory
    from equitypulse.prices import load_bhavcopy

    settings = get_settings()
    delay = settings.nse_request_delay_seconds

    with session_factory()() as session:
        try:
            cal = NseCalendar.from_session(session)
        except Exception:
            typer.echo(
                "Calendar not loaded. Run `ep nightly` or refresh the calendar first.",
                err=True,
            )
            raise typer.Exit(1) from None

        date = from_date
        while date <= to_date:
            if cal.is_trading_day(date):
                typer.echo(f"  {date} … ", nl=False)
                try:
                    result = load_bhavcopy(session, date)
                    session.commit()
                    typer.echo(f"+{result.inserted} rows")
                except Exception as exc:
                    session.rollback()
                    typer.echo(f"FAILED: {exc}", err=True)
                time.sleep(delay)
            else:
                typer.echo(f"  {date} skip (not a trading day)")
            date += dt.timedelta(days=1)


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
