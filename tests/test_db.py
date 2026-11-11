"""Schema tests. Every test migrates a fresh temp SQLite DB with Alembic (never the real DB,
never the real .env) so the migration itself is what gets tested, not `create_all`."""

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, insert, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.orm import Session

from equitypulse.db.models import (
    ApiUsage,
    AppendOnlyError,
    Base,
    CorporateAction,
    DataAudit,
    IndexMembership,
    NameMap,
    PriceDaily,
    Security,
    SmallcaseEvent,
    SmallcaseIndex,
    SmallcasePeriod,
    Snapshot,
    Surveillance,
    SymbolHistory,
    TradingCalendar,
)
from equitypulse.db.session import make_engine

REPO = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FETCHED = dt.datetime(2026, 11, 10, 12, 30, tzinfo=dt.UTC)
D = dt.date(2026, 11, 10)
ISIN = "INE002A01018"

SPEC_TABLES = {
    "securities",
    "symbol_history",
    "trading_calendar",
    "prices_daily",
    "corporate_actions",
    "surveillance",
    "index_membership",
    "snapshots",
    "data_audit",
    "api_usage",
    "smallcase_periods",
    "smallcase_events",
    "smallcase_index",
    "name_map",
}

# The natural key of each table: re-inserting a row with the same key must fail.
NATURAL_KEYS: dict[str, set[str]] = {
    "securities": {"isin"},
    "symbol_history": {"isin", "valid_from"},
    "trading_calendar": {"date"},
    "prices_daily": {"isin", "date"},
    "corporate_actions": {"isin", "ex_date", "action_type"},
    "surveillance": {"as_of", "isin", "list_type"},
    "index_membership": {"index_name", "isin", "date_from"},
    "snapshots": {"source", "endpoint", "entity", "as_of", "payload_hash"},
    "data_audit": {"run_id", "job", "source", "status"},
    "api_usage": {"date", "source"},
    "smallcase_periods": {"smallcase", "period_start", "raw_name"},
    "smallcase_events": {"smallcase", "date", "raw_name", "action"},
    "smallcase_index": {"smallcase", "date"},
    "name_map": {"raw_name"},
}

# Columns later phases filter by: each must lead an index (or the primary key).
FILTER_COLUMNS: dict[str, set[str]] = {
    "prices_daily": {"isin", "date"},
    "surveillance": {"as_of", "isin"},
    "corporate_actions": {"isin", "ex_date"},
    "index_membership": {"isin"},
    "snapshots": {"as_of"},
    "data_audit": {"as_of", "run_id"},
    "smallcase_periods": {"smallcase", "isin"},
    "smallcase_events": {"smallcase", "date", "isin"},
    "smallcase_index": {"smallcase", "date"},
    "name_map": {"isin"},
    "symbol_history": {"isin", "symbol"},
    "securities": {"symbol"},
}


def sample_rows() -> dict[type[Base], dict[str, Any]]:
    """One valid row per table, parents first (dict order is insertion order)."""
    return {
        Security: dict(
            isin=ISIN,
            symbol="RELIANCE",
            name="Reliance Industries Limited",
            series="EQ",
            sector="Energy",
            industry="Refineries",
            listing_date=dt.date(1995, 11, 29),
            status="ACTIVE",
            source="NSE_EQUITY_L",
            as_of=D,
            fetched_at=FETCHED,
        ),
        SymbolHistory: dict(
            isin=ISIN,
            symbol="RELIANCE",
            valid_from=dt.date(1995, 11, 29),
            valid_to=None,
            source="NSE_EQUITY_L",
            fetched_at=FETCHED,
        ),
        TradingCalendar: dict(date=D, is_trading_day=True, note=None, source="NSE_HOLIDAYS"),
        PriceDaily: dict(
            isin=ISIN,
            date=D,
            open=1490.0,
            high=1512.5,
            low=1485.1,
            close=1501.35,
            prev_close=1488.2,
            volume=12_345_678_901,  # > 2**31: must survive as BigInteger
            traded_value=1.85e10,
            delivery_qty=6_000_000_000,
            delivery_pct=0.486,
            series="EQ",
            source="NSE_BHAVCOPY",
            fetched_at=FETCHED,
        ),
        CorporateAction: dict(
            isin=ISIN,
            ex_date=dt.date(2024, 10, 28),
            action_type="BONUS",
            details="1:1 bonus",
            adj_factor=0.5,
            source="TAPETIDE",
            fetched_at=FETCHED,
        ),
        Surveillance: dict(
            as_of=D,
            isin=ISIN,
            list_type="ASM",
            stage="LTASM Stage I",
            source="NSE",
            fetched_at=FETCHED,
        ),
        IndexMembership: dict(
            index_name="NIFTY 500",
            isin=ISIN,
            date_from=dt.date(2026, 9, 30),
            date_to=None,
            source="NIFTYINDICES",
            fetched_at=FETCHED,
        ),
        Snapshot: dict(
            source="TAPETIDE",
            endpoint="get_company_profile",
            entity=ISIN,
            as_of=D,
            fetched_at=FETCHED,
            payload_json='{"name": "Reliance"}',
            payload_hash="a" * 64,
        ),
        DataAudit: dict(
            run_id="3f2b9c1e-0000-4000-8000-000000000001",
            job="nightly",
            source="NSE_BHAVCOPY",
            as_of=D,
            started_at=FETCHED,
            finished_at=FETCHED + dt.timedelta(minutes=3),
            status="DEGRADED",
            rows_in=2100,
            rows_written=2095,
            stale_count=1,
            anomaly_count=4,
            error="5 rows had zero volume",
        ),
        ApiUsage: dict(date=D, source="TAPETIDE", calls=12),
        SmallcasePeriod: dict(
            smallcase="Dividend Aristocrats",
            period_start=dt.date(2024, 1, 1),
            period_end=dt.date(2024, 6, 30),
            raw_name="Reliance Industries",
            isin=ISIN,
            weight=0.0725,
        ),
        SmallcaseEvent: dict(
            smallcase="Dividend Aristocrats",
            date=dt.date(2024, 1, 1),
            raw_name="Reliance Industries",
            isin=ISIN,
            action="INITIAL",
            weight=0.0725,
        ),
        SmallcaseIndex: dict(smallcase="Dividend Aristocrats", date=D, index_value=187.42),
        NameMap: dict(
            raw_name="Reliance Industries", isin=ISIN, method="exact", confidence=1.0, verified=True
        ),
    }


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # so nothing can pick up the project's .env


def alembic_config(url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def add_parent_security(session: Session) -> None:
    session.add(Security(**sample_rows()[Security]))
    session.commit()


# --- migration ---------------------------------------------------------------------------


def test_upgrade_head_creates_every_spec_table(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == SPEC_TABLES
    assert set(Base.metadata.tables) == SPEC_TABLES


def test_migration_matches_models(engine: Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_downgrade_base_drops_everything(db_url: str, engine: Engine) -> None:
    command.downgrade(alembic_config(db_url), "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()


def test_upgrade_is_idempotent(db_url: str, engine: Engine) -> None:
    command.upgrade(alembic_config(db_url), "head")  # second run: no-op, no error
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == SPEC_TABLES


# --- engine ------------------------------------------------------------------------------


def test_sqlite_connections_use_wal_and_foreign_keys(engine: Engine) -> None:
    for _ in range(2):  # every new connection, not just the first
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


# --- round trip --------------------------------------------------------------------------


def test_one_row_round_trips_per_table(session: Session) -> None:
    rows = sample_rows()
    assert {m.__tablename__ for m in rows} == SPEC_TABLES
    identities = {}
    for model, values in rows.items():
        obj = model(**values)
        session.add(obj)
        session.flush()
        identities[model] = inspect(obj).identity
    session.commit()
    session.expunge_all()

    for model, values in rows.items():
        loaded = session.get(model, identities[model])
        assert loaded is not None, model.__tablename__
        for column, expected in values.items():
            assert getattr(loaded, column) == expected, f"{model.__tablename__}.{column}"


def test_timestamps_come_back_timezone_aware_utc(session: Session) -> None:
    values = sample_rows()[Security] | {"fetched_at": dt.datetime(2026, 11, 10, 18, 0, tzinfo=IST)}
    session.add(Security(**values))
    session.commit()
    session.expunge_all()
    loaded = session.get(Security, ISIN)
    assert loaded is not None
    assert loaded.fetched_at.tzinfo == dt.UTC
    assert loaded.fetched_at == dt.datetime(2026, 11, 10, 12, 30, tzinfo=dt.UTC)


def test_naive_timestamp_is_rejected(session: Session) -> None:
    values = sample_rows()[Security] | {"fetched_at": dt.datetime(2026, 11, 10, 12, 30)}
    session.add(Security(**values))
    with pytest.raises(StatementError, match="timezone-aware"):
        session.flush()


# --- constraints -------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(NATURAL_KEYS))
def test_schema_declares_natural_key(engine: Engine, table: str) -> None:
    insp = inspect(engine)
    keys = [set(insp.get_pk_constraint(table)["constrained_columns"])]
    keys += [set(uq["column_names"]) for uq in insp.get_unique_constraints(table)]
    assert NATURAL_KEYS[table] in keys


@pytest.mark.parametrize("model", list(sample_rows()), ids=lambda m: m.__tablename__)
def test_duplicate_natural_key_is_rejected(session: Session, model: type[Base]) -> None:
    if model is not Security:
        add_parent_security(session)
    values = {k: v for k, v in sample_rows()[model].items() if k != "id"}
    table = model.__table__
    session.execute(insert(table).values(**values))
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(insert(table).values(**values))
    session.rollback()


@pytest.mark.parametrize("table", sorted(FILTER_COLUMNS))
def test_filter_columns_are_indexed(engine: Engine, table: str) -> None:
    insp = inspect(engine)
    leading = {ix["column_names"][0] for ix in insp.get_indexes(table)}
    leading |= {uq["column_names"][0] for uq in insp.get_unique_constraints(table)}
    pk = insp.get_pk_constraint(table)["constrained_columns"]
    if pk:
        leading.add(pk[0])
    missing = FILTER_COLUMNS[table] - leading
    assert not missing, f"{table}: no index leads with {missing}"


def test_check_constraint_rejects_bad_audit_status(session: Session) -> None:
    values = sample_rows()[DataAudit] | {"status": "BROKEN"}
    session.add(DataAudit(**values))
    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


@pytest.mark.parametrize(
    ("model", "override"),
    [
        (Surveillance, {"list_type": "XYZ"}),
        (SmallcaseEvent, {"action": "BUY"}),
        (CorporateAction, {"action_type": "SPLITT"}),
        (NameMap, {"method": "guess"}),
        (PriceDaily, {"delivery_pct": 48.6}),  # a percentage, not a fraction
        (SmallcasePeriod, {"weight": 7.25}),
        (ApiUsage, {"calls": -1}),
        (IndexMembership, {"date_to": dt.date(2026, 1, 1)}),  # ends before it starts
        (Security, {"isin": "INE002A0101"}),  # 11 chars
    ],
    ids=lambda p: p.__tablename__ if isinstance(p, type) else next(iter(p)),
)
def test_check_constraints_reject_bad_values(
    session: Session, model: type[Base], override: dict[str, Any]
) -> None:
    if model is not Security:
        add_parent_security(session)
    session.add(model(**(sample_rows()[model] | override)))
    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_foreign_key_rejects_unknown_isin(session: Session) -> None:
    session.add(PriceDaily(**sample_rows()[PriceDaily]))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


# --- append-only -------------------------------------------------------------------------


@pytest.mark.parametrize("model", [Snapshot, DataAudit], ids=lambda m: m.__tablename__)
def test_append_only_rows_cannot_be_updated(session: Session, model: type[Base]) -> None:
    obj = model(**sample_rows()[model])
    session.add(obj)
    session.commit()
    obj.source = "CHANGED"
    with pytest.raises(AppendOnlyError):
        session.flush()


@pytest.mark.parametrize("model", [Snapshot, DataAudit], ids=lambda m: m.__tablename__)
def test_append_only_rows_cannot_be_deleted(session: Session, model: type[Base]) -> None:
    obj = model(**sample_rows()[model])
    session.add(obj)
    session.commit()
    session.delete(obj)
    with pytest.raises(AppendOnlyError):
        session.flush()


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("snapshots", "UPDATE snapshots SET payload_json = '{}'"),
        ("snapshots", "DELETE FROM snapshots"),
        ("data_audit", "UPDATE data_audit SET status = 'OK'"),
        ("data_audit", "DELETE FROM data_audit"),
    ],
    ids=["snapshots-update", "snapshots-delete", "data_audit-update", "data_audit-delete"],
)
def test_db_trigger_blocks_raw_sql_changes(engine: Engine, table: str, sql: str) -> None:
    model = Snapshot if table == "snapshots" else DataAudit
    with engine.begin() as conn:
        conn.execute(insert(model.__table__).values(**sample_rows()[model]))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(sql))
    with engine.connect() as conn:  # the row is untouched
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 1


def test_same_payload_on_later_as_of_is_stored_again(session: Session) -> None:
    first = sample_rows()[Snapshot]
    later = first | {"as_of": D + dt.timedelta(days=1)}
    session.add_all([Snapshot(**first), Snapshot(**later)])
    session.commit()
    assert session.query(Snapshot).count() == 2


def test_audit_run_has_started_and_final_rows(session: Session) -> None:
    final = sample_rows()[DataAudit]
    started = final | {
        "status": "STARTED",
        "finished_at": None,
        "rows_in": 0,
        "rows_written": 0,
        "stale_count": 0,
        "anomaly_count": 0,
        "error": None,
    }
    session.add_all([DataAudit(**started), DataAudit(**final)])
    session.commit()
    statuses = {a.status for a in session.query(DataAudit).filter_by(run_id=final["run_id"])}
    assert statuses == {"STARTED", "DEGRADED"}


@pytest.mark.parametrize(
    "override",
    [
        {"status": "STARTED"},  # STARTED must not have finished_at
        {"status": "OK", "finished_at": None},  # a final row must have finished_at
    ],
    ids=["started-with-finish", "final-without-finish"],
)
def test_audit_finished_at_matches_status(session: Session, override: dict[str, Any]) -> None:
    session.add(DataAudit(**(sample_rows()[DataAudit] | override)))
    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_unmapped_names_can_be_stored(session: Session) -> None:
    rows = sample_rows()
    session.add_all(
        [
            NameMap(raw_name="Mystery Ltd", isin=None, method=None, confidence=None),
            SmallcasePeriod(**(rows[SmallcasePeriod] | {"raw_name": "Mystery Ltd", "isin": None})),
            SmallcaseEvent(**(rows[SmallcaseEvent] | {"raw_name": "Mystery Ltd", "isin": None})),
        ]
    )
    session.commit()  # no securities row exists, and none is needed when isin is NULL
    assert session.get(NameMap, "Mystery Ltd").verified is False  # type: ignore[union-attr]


def test_ordinary_tables_can_still_be_updated(session: Session) -> None:
    session.add(ApiUsage(**sample_rows()[ApiUsage]))
    session.commit()
    usage = session.get(ApiUsage, (D, "TAPETIDE"))
    assert usage is not None
    usage.calls += 1
    session.commit()
    assert session.get(ApiUsage, (D, "TAPETIDE")).calls == 13  # type: ignore[union-attr]
