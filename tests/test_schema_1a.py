"""T16: Phase 1a tables — append-only ledger, and the upgrade from a Phase 0 database."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import insert, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from equitypulse.db.models import (
    AppendOnlyError,
    Decision,
    ParamSet,
    PriceDaily,
    Recommendation,
    Scan,
    Security,
    Snapshot,
)
from equitypulse.db.session import make_engine
from tests.test_db import alembic_config, sample_rows

PHASE0_HEAD = "664b9d237a67"
LEDGER = [ParamSet, Recommendation, Decision]


def _seed_ledger(session: Session) -> None:
    rows = sample_rows()
    for model in (Security, ParamSet, Scan, Recommendation, Decision):
        session.add(model(**rows[model]))
        session.flush()
    session.commit()


@pytest.fixture
def session(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'ep.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = make_engine(url)
    with Session(engine) as s:
        yield s
    engine.dispose()


_CHANGE = {ParamSet: ("yaml", "version: v1\nedited: true\n"),
           Recommendation: ("rationale", "rewritten after the fact"),
           Decision: ("reason", "changed my mind")}


@pytest.mark.parametrize("model", LEDGER, ids=lambda m: m.__tablename__)
def test_ledger_rows_cannot_be_updated_through_the_orm(session: Session, model) -> None:
    _seed_ledger(session)
    obj = session.query(model).one()
    column, value = _CHANGE[model]
    setattr(obj, column, value)
    with pytest.raises(AppendOnlyError, match="append-only"):
        session.flush()


@pytest.mark.parametrize("model", LEDGER, ids=lambda m: m.__tablename__)
def test_ledger_rows_cannot_be_deleted_through_the_orm(session: Session, model) -> None:
    _seed_ledger(session)
    session.delete(session.query(model).one())
    with pytest.raises(AppendOnlyError, match="append-only"):
        session.flush()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE recommendations SET stop = 1",
        "DELETE FROM recommendations",
        "UPDATE param_sets SET yaml = 'x'",
        "DELETE FROM param_sets",
        "UPDATE decisions SET action = 'TAKE'",
        "DELETE FROM decisions",
    ],
)
def test_db_trigger_blocks_raw_sql_on_the_ledger(session: Session, sql: str) -> None:
    _seed_ledger(session)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(sql))
    session.rollback()
    table = sql.split()[1] if sql.startswith("UPDATE") else sql.split()[2]
    assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 1


def test_a_changed_mind_is_a_new_decision_row(session: Session) -> None:
    _seed_ledger(session)
    session.add(Decision(recommendation_id=1, action="TAKE", reason=None))
    session.commit()
    assert [d.action for d in session.query(Decision).order_by(Decision.id)] == ["SKIP", "TAKE"]


def test_upgrade_from_phase0_keeps_every_row(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'phase0.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, PHASE0_HEAD)
    engine = make_engine(url)
    rows = sample_rows()
    with engine.begin() as conn:
        for model in (Security, PriceDaily, Snapshot):
            conn.execute(insert(model.__table__).values(**rows[model]))
        before = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
            for t in ("securities", "prices_daily", "snapshots")
        }
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        after = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in before}
        tables = set(inspect(conn).get_table_names())
    engine.dispose()
    assert after == before == {"securities": 1, "prices_daily": 1, "snapshots": 1}
    assert {"fundamentals", "features", "recommendations", "decisions"} <= tables
