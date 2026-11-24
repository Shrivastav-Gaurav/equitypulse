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
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-download dates already loaded (upsert; unchanged rows skip)"
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
    from equitypulse.isin_links import ChainUniverse
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
            typer.echo(f"Universe {universe}: {len(members)} ISINs (following ISIN changes)")
            members = ChainUniverse.following_chains(session, members)

        result = run_backfill(
            session, cal, from_date, to_date, universe=members, delay_seconds=delay,
            refresh=refresh, progress=lambda msg: typer.echo(f"  {msg}"),
        )

    typer.echo(
        f"Backfill: loaded {result.loaded} dates ({result.rows_written} rows), "
        f"already loaded {result.already_loaded}, failed {len(result.failed)}, "
        f"ISIN changes linked {result.isin_links_added}, "
        f"calendar mismatches {len(result.calendar_mismatches)}"
    )
    for d in result.calendar_mismatches:
        typer.echo(f"  calendar mismatch {d} (see data_audit job calendar_check)", err=True)
    if result.blocked:
        typer.echo("STOPPED: NSE refused repeated downloads; it may be blocking us.", err=True)
        raise typer.Exit(2)
    if result.failed or result.calendar_mismatches:
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


@app.command("symbol-changes")
def symbol_changes() -> None:
    """Load NSE's symbol-change list into symbol_history, then re-run ISIN link detection."""
    from equitypulse.audit import AuditContext
    from equitypulse.calendar import today_ist
    from equitypulse.db.session import session_factory
    from equitypulse.isin_links import detect_links
    from equitypulse.sources.nse_actions import SOURCE_SYMBOLS
    from equitypulse.symbol_changes import load_symbol_changes

    with session_factory()() as session:
        with AuditContext(
            session, job="symbol_changes", source=SOURCE_SYMBOLS, as_of=today_ist()
        ) as ctx:
            result = load_symbol_changes(session)
            links = detect_links(session)
            ctx.rows_in = result.changes_in_file
            ctx.rows_written = result.inserted + links
        session.commit()
    typer.echo(
        f"Symbol changes: {result.changes_in_file} in file, {result.inserted} history rows "
        f"added, {result.already_present} already present; ISIN links added {links}"
    )


@app.command("corporate-actions")
def corporate_actions(
    from_date: dt.date = typer.Option(  # noqa: B008
        ..., "--from", formats=["%Y-%m-%d"], help="First ex-date (inclusive)"
    ),
    to_date: dt.date = typer.Option(  # noqa: B008
        ..., "--to", formats=["%Y-%m-%d"], help="Last ex-date (inclusive)"
    ),
) -> None:
    """Load NSE split/bonus/rights/demerger actions for securities we hold prices for."""
    from equitypulse.audit import AuditContext
    from equitypulse.corporate_actions_nse import load_nse_corporate_actions
    from equitypulse.db.session import session_factory
    from equitypulse.sources.nse_actions import SOURCE_ACTIONS

    with session_factory()() as session:
        with AuditContext(
            session, job="corporate_actions", source=SOURCE_ACTIONS, as_of=to_date
        ) as ctx:
            result = load_nse_corporate_actions(session, from_date, to_date)
            ctx.rows_in = result.rows_in
            ctx.rows_written = result.inserted
            ctx.anomaly_count = result.unparsed
            ctx.degraded = result.unparsed > 0
        session.commit()
    typer.echo(
        f"Corporate actions {from_date}..{to_date}: {result.rows_in} rows, {result.inserted} "
        f"stored, {result.already_present} already present, {result.not_in_universe} outside "
        f"our securities, {result.skipped_kinds} dividends/other skipped, {result.unparsed} "
        "unparsed split/bonus mentions (stored as OTHER, no factor)"
    )


@app.command("manual-data")
def manual_data() -> None:
    """Apply config/manual_data.yaml: explained gaps and links, hand-added demergers."""
    from equitypulse.db.session import session_factory
    from equitypulse.manual_data import apply_manual_data, load_manual_data

    with session_factory()() as session:
        result = apply_manual_data(session, load_manual_data())
        session.commit()
    typer.echo(
        f"Manual data: {result.actions_added} actions added, {result.links_updated} links "
        f"explained, {result.audit_rows_added} data_audit rows written"
    )


@app.command()
def coverage(
    from_date: dt.date = typer.Option(  # noqa: B008
        ..., "--from", formats=["%Y-%m-%d"], help="First day (inclusive)"
    ),
    to_date: dt.date = typer.Option(  # noqa: B008
        ..., "--to", formats=["%Y-%m-%d"], help="Last day (inclusive)"
    ),
    index: str = typer.Option("NIFTY500", "--index", help="Current members of this index"),
) -> None:
    """Missing price rows for an index's current members, and which gaps are explained."""
    from equitypulse.coverage import price_coverage
    from equitypulse.db.session import session_factory
    from equitypulse.index_load import open_members
    from equitypulse.manual_data import load_manual_data

    with session_factory()() as session:
        report = price_coverage(
            session, open_members(session, index), from_date, to_date,
            gap_notes=load_manual_data().gaps,
        )
    typer.echo(
        f"{index} {from_date}..{to_date}: {report.securities} securities, expected "
        f"{report.expected} rows, missing {report.missing} ({report.missing_pct:.4%}), "
        f"explained {report.explained}, unexplained {report.unexplained} "
        f"({report.unexplained_pct:.4%})"
    )
    typer.echo(f"  no equity rows: {', '.join(report.no_rows) or 'none'}")
    for g in report.late_starts:
        typer.echo(
            f"  late start: {g.symbol} expected from {g.expected_start}, first row {g.first_row}"
        )
    for g in report.gaps:
        typer.echo(
            f"  gap: {g.symbol} {g.missing} days ({g.first_missing}..{g.last_missing}), "
            f"explained {g.explained}"
        )
    if report.unexplained:
        raise typer.Exit(1)


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
