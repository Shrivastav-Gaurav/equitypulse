"""SQLAlchemy models for the Phase 0 schema (docs/phase0_spec.md).

Design rules this schema enforces, so later code can't quietly break them:
- Idempotency: every table has a primary or unique key on its natural key, so re-running a
  job for the same date can't duplicate rows. Loaders upsert on these keys.
- Point-in-time: fetched rows carry `source` and `fetched_at` (plus `as_of` where the source
  has a date of its own).
- Units: prices are floats in INR, volumes are BigInteger, and weights and percentages are
  fractions (0..1), which CHECK constraints enforce so a stray 48.6 can't pass for 48.6%.
- ISIN is the security key; other tables reference `securities.isin`, except `surveillance`
  (NSE lists include SME/other-series stocks that are not in the equity master).
- `snapshots`, `data_audit`, `param_sets`, `recommendations` and `decisions` are append-only:
  the ORM refuses updates and deletes on them,
  and database triggers (created in the migration) refuse them from raw SQL too.
"""

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from equitypulse.db.types import UTCDateTime, utcnow

AUDIT_STATUSES = ("STARTED", "OK", "DEGRADED", "FAILED")
SURVEILLANCE_LISTS = ("ASM", "GSM", "ESM")
SMALLCASE_ACTIONS = ("ADD", "REMOVE", "INITIAL")
CORPORATE_ACTION_TYPES = ("SPLIT", "BONUS", "RIGHTS", "DEMERGER", "DIVIDEND", "MERGER", "OTHER")
NAME_MAP_METHODS = ("exact", "fuzzy", "manual")

# Deterministic constraint names: Alembic batch mode on SQLite needs them, and PostgreSQL
# migrations later can then refer to constraints by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ISIN = String(12)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _fraction(column: str, *, nullable: bool = True) -> str:
    rule = f"{column} >= 0 AND {column} <= 1"
    return f"{column} IS NULL OR ({rule})" if nullable else rule


def _isin_fk() -> ForeignKey:
    return ForeignKey("securities.isin")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dt.datetime: UTCDateTime()}


class Security(Base):
    __tablename__ = "securities"
    __table_args__ = (CheckConstraint("length(isin) = 12", name="isin_length"),)

    isin: Mapped[str] = mapped_column(ISIN, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255))
    series: Mapped[str | None] = mapped_column(String(8))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    listing_date: Mapped[dt.date | None]
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    source: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[dt.date]
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class SymbolHistory(Base):
    """Symbol each ISIN traded under over time; renames and recycled symbols."""

    __tablename__ = "symbol_history"
    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="valid_range"),
    )

    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True)
    valid_from: Mapped[dt.date] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    valid_to: Mapped[dt.date | None]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class IsinLink(Base):
    """`old_isin` was replaced by `new_isin` for the same security from `effective_date`.

    NSE issues a new ISIN on a face-value split and similar events, so one security can span
    several ISINs. Prices stay keyed by the ISIN they traded under; readers follow the chain.
    """

    __tablename__ = "isin_links"
    __table_args__ = (CheckConstraint("old_isin <> new_isin", name="distinct_isins"),)

    old_isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True)
    new_isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True, index=True)
    effective_date: Mapped[dt.date]
    reason: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    is_trading_day: Mapped[bool]
    note: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32))


class PriceDaily(Base):
    """Raw, unadjusted daily prices; adjustment uses `corporate_actions` at read time."""

    __tablename__ = "prices_daily"
    __table_args__ = (
        CheckConstraint(_fraction("delivery_pct"), name="delivery_pct_fraction"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("delivery_qty IS NULL OR delivery_qty >= 0", name="delivery_qty_non_neg"),
    )

    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True)
    date: Mapped[dt.date] = mapped_column(primary_key=True, index=True)
    open: Mapped[float]
    high: Mapped[float]
    low: Mapped[float]
    close: Mapped[float]
    prev_close: Mapped[float | None]
    volume: Mapped[int] = mapped_column(BigInteger)
    traded_value: Mapped[float | None]
    delivery_qty: Mapped[int | None] = mapped_column(BigInteger)
    delivery_pct: Mapped[float | None]
    series: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class CorporateAction(Base):
    """Splits/bonuses carry `adj_factor`; rights/demergers are flagged with no factor."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("isin", "ex_date", "action_type"),
        CheckConstraint(_one_of("action_type", CORPORATE_ACTION_TYPES), name="action_type"),
        CheckConstraint("adj_factor IS NULL OR adj_factor > 0", name="adj_factor_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), index=True)
    ex_date: Mapped[dt.date] = mapped_column(index=True)
    action_type: Mapped[str] = mapped_column(String(16))
    details: Mapped[str | None] = mapped_column(Text)
    adj_factor: Mapped[float | None]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Surveillance(Base):
    """One row per stock per list per date. No FK: lists include SME stocks not in master."""

    __tablename__ = "surveillance"
    __table_args__ = (CheckConstraint(_one_of("list_type", SURVEILLANCE_LISTS), name="list_type"),)

    as_of: Mapped[dt.date] = mapped_column(primary_key=True)
    isin: Mapped[str] = mapped_column(ISIN, primary_key=True, index=True)
    list_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    stage: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class IndexMembership(Base):
    """Open membership has `date_to` NULL; a refresh closes it rather than deleting it."""

    __tablename__ = "index_membership"
    __table_args__ = (
        UniqueConstraint("index_name", "isin", "date_from"),
        CheckConstraint("date_to IS NULL OR date_to >= date_from", name="valid_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    index_name: Mapped[str] = mapped_column(String(64))
    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), index=True)
    date_from: Mapped[dt.date]
    date_to: Mapped[dt.date | None]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Snapshot(Base):
    """Append-only raw payloads. An identical payload for the same as_of dedupes on hash; the
    same payload observed on a later as_of is stored again, so "what did we know on D" holds.

    `entity` and `as_of` are NOT NULL because NULLs never collide in a unique key, which would
    silently disable dedupe: use '' for calls with no single entity, and the IST fetch date
    as `as_of` for calls that return current data.
    """

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("source", "endpoint", "entity", "as_of", "payload_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(128))
    entity: Mapped[str] = mapped_column(String(128), default="")
    as_of: Mapped[dt.date] = mapped_column(index=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))


class DataAudit(Base):
    """Append-only run log. A job writes a STARTED row when it begins and a second, final row
    (OK / DEGRADED / FAILED) when it ends; a STARTED row with no final row is a crashed run.
    `as_of` is the market date the run was for, so `ep status` and reruns can find it."""

    __tablename__ = "data_audit"
    __table_args__ = (
        UniqueConstraint("run_id", "job", "source", "status"),
        CheckConstraint(_one_of("status", AUDIT_STATUSES), name="status"),
        CheckConstraint(
            "(status = 'STARTED' AND finished_at IS NULL)"
            " OR (status <> 'STARTED' AND finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        CheckConstraint(
            "rows_in >= 0 AND rows_written >= 0 AND stale_count >= 0 AND anomaly_count >= 0",
            name="counts_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    job: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[dt.date | None] = mapped_column(index=True)
    started_at: Mapped[dt.datetime] = mapped_column(index=True)
    finished_at: Mapped[dt.datetime | None]
    status: Mapped[str] = mapped_column(String(16))
    rows_in: Mapped[int] = mapped_column(default=0)
    rows_written: Mapped[int] = mapped_column(default=0)
    stale_count: Mapped[int] = mapped_column(default=0)
    anomaly_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ApiUsage(Base):
    """Budget-guard counter; updated in place (not append-only).

    `date` is the Indian calendar date (Asia/Kolkata), not the UTC date: the daily budget
    resets at IST midnight, and between 00:00 and 05:30 IST the UTC date is still yesterday.
    The budget guard must compute it as `datetime.now(ZoneInfo("Asia/Kolkata")).date()`.
    """

    __tablename__ = "api_usage"
    __table_args__ = (CheckConstraint("calls >= 0", name="calls_non_negative"),)

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    calls: Mapped[int] = mapped_column(default=0)


class SmallcasePeriod(Base):
    __tablename__ = "smallcase_periods"
    __table_args__ = (
        UniqueConstraint("smallcase", "period_start", "raw_name"),
        CheckConstraint(_fraction("weight"), name="weight_fraction"),
        CheckConstraint("period_end IS NULL OR period_end >= period_start", name="valid_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    smallcase: Mapped[str] = mapped_column(String(128))
    period_start: Mapped[dt.date]
    period_end: Mapped[dt.date | None]
    raw_name: Mapped[str] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(ISIN, _isin_fk(), index=True)
    weight: Mapped[float | None]


class SmallcaseEvent(Base):
    __tablename__ = "smallcase_events"
    __table_args__ = (
        UniqueConstraint("smallcase", "date", "raw_name", "action"),
        CheckConstraint(_one_of("action", SMALLCASE_ACTIONS), name="action"),
        CheckConstraint(_fraction("weight"), name="weight_fraction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    smallcase: Mapped[str] = mapped_column(String(128))
    date: Mapped[dt.date] = mapped_column(index=True)
    raw_name: Mapped[str] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(ISIN, _isin_fk(), index=True)
    action: Mapped[str] = mapped_column(String(8))
    weight: Mapped[float | None]


class SmallcaseIndex(Base):
    __tablename__ = "smallcase_index"
    __table_args__ = (CheckConstraint("index_value > 0", name="index_value_positive"),)

    smallcase: Mapped[str] = mapped_column(String(128), primary_key=True)
    date: Mapped[dt.date] = mapped_column(primary_key=True, index=True)
    index_value: Mapped[float]


class NameMap(Base):
    """Smallcase raw name -> ISIN. Unmapped names keep `isin` and `method` NULL."""

    __tablename__ = "name_map"
    __table_args__ = (
        CheckConstraint(f"method IS NULL OR {_one_of('method', NAME_MAP_METHODS)}", name="method"),
        CheckConstraint(_fraction("confidence"), name="confidence_fraction"),
    )

    raw_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    isin: Mapped[str | None] = mapped_column(ISIN, _isin_fk(), index=True)
    method: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None]
    verified: Mapped[bool] = mapped_column(default=False)


# --- Phase 1a ----------------------------------------------------------------------------

HORIZONS = ("1m", "3m", "6m", "10d")
SCAN_HORIZONS = ("1m", "3m", "6m")  # 10d is research-only: never on a card
DECISION_ACTIONS = ("TAKE", "SKIP")


def _percentile(column: str) -> str:
    return f"{column} IS NULL OR ({column} >= 0 AND {column} <= 100)"


class Fundamental(Base):
    """One reported number per filing. A restatement is a new row; the original stays.

    A factor for date D reads the latest row with `filing_date <= D`. When the source gives no
    filing date, `filing_date` is the SEBI results deadline (quarter end + 45 days, Q4 + 60)
    and `filing_date_estimated` is true, so the lag is never shorter than reality allowed.
    """

    __tablename__ = "fundamentals"

    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True)
    period_end: Mapped[dt.date] = mapped_column(primary_key=True)
    statement: Mapped[str] = mapped_column(String(32), primary_key=True)
    field: Mapped[str] = mapped_column(String(64), primary_key=True)
    filing_date: Mapped[dt.date] = mapped_column(primary_key=True, index=True)
    filing_date_estimated: Mapped[bool] = mapped_column(default=False)
    value: Mapped[float]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class IndexLevel(Base):
    """Daily index close (and total-return close where published), and India VIX."""

    __tablename__ = "index_levels"
    __table_args__ = (
        CheckConstraint("close > 0", name="close_positive"),
        CheckConstraint("tri_close IS NULL OR tri_close > 0", name="tri_close_positive"),
    )

    index_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[dt.date] = mapped_column(primary_key=True, index=True)
    close: Mapped[float]
    tri_close: Mapped[float | None]
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class ParamSet(Base):
    """A registered parameter file. Append-only: a changed file must get a new version."""

    __tablename__ = "param_sets"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    yaml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Feature(Base):
    """Factor inputs, sector percentiles and composites for one stock on one date.

    Idempotent per (isin, as_of, params_version). Percentiles are 0-100; a factor that could
    not be scored is NULL, never 0.
    """

    __tablename__ = "features"
    __table_args__ = tuple(
        CheckConstraint(_percentile(c), name=f"{c}_range")
        for c in (
            "pct_momentum", "pct_volume_delivery", "pct_reported_growth", "pct_quality",
            "pct_valuation", "pct_earnings_revisions", "pct_flows",
            "composite_1m", "composite_3m", "composite_6m", "composite_10d",
        )
    )

    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), primary_key=True)
    as_of: Mapped[dt.date] = mapped_column(primary_key=True, index=True)
    params_version: Mapped[str] = mapped_column(
        String(32), ForeignKey("param_sets.version"), primary_key=True
    )
    sector_group: Mapped[str | None] = mapped_column(String(128))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    pct_momentum: Mapped[float | None]
    pct_volume_delivery: Mapped[float | None]
    pct_reported_growth: Mapped[float | None]
    pct_quality: Mapped[float | None]
    pct_valuation: Mapped[float | None]
    pct_earnings_revisions: Mapped[float | None]
    pct_flows: Mapped[float | None]
    composite_1m: Mapped[float | None]
    composite_3m: Mapped[float | None]
    composite_6m: Mapped[float | None]
    composite_10d: Mapped[float | None]
    factors_scored: Mapped[int] = mapped_column(default=0)
    frameworks: Mapped[dict[str, Any]] = mapped_column(JSON)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON)
    data_ok: Mapped[bool]
    computed_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Scan(Base):
    """Every scan request, stored before it runs, including ones that return no cards."""

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    as_of: Mapped[dt.date] = mapped_column(index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    params_version: Mapped[str] = mapped_column(String(32), ForeignKey("param_sets.version"))
    data_snapshot_ids: Mapped[list[int]] = mapped_column(JSON)
    regime: Mapped[str] = mapped_column(String(16))


class Recommendation(Base):
    """The prediction ledger: one trade card, written before it is shown. Append-only."""

    __tablename__ = "recommendations"
    __table_args__ = (
        CheckConstraint(_one_of("horizon", SCAN_HORIZONS), name="horizon"),
        CheckConstraint("stop > 0 AND stop < entry AND entry < target", name="price_order"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint(_fraction("hit_p"), name="hit_p_fraction"),
        CheckConstraint(_fraction("hit_low"), name="hit_low_fraction"),
        CheckConstraint(_fraction("hit_high"), name="hit_high_fraction"),
        CheckConstraint("hit_n IS NULL OR hit_n >= 0", name="hit_n_non_negative"),
        CheckConstraint(_percentile("composite"), name="composite_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), index=True)
    isin: Mapped[str] = mapped_column(ISIN, _isin_fk(), index=True)
    horizon: Mapped[str] = mapped_column(String(4))
    entry: Mapped[float]
    target: Mapped[float]
    stop: Mapped[float]
    qty: Mapped[int]
    exit_by: Mapped[dt.date]
    composite: Mapped[float]
    factor_scores: Mapped[dict[str, Any]] = mapped_column(JSON)
    hit_p: Mapped[float | None]
    hit_low: Mapped[float | None]
    hit_high: Mapped[float | None]
    hit_n: Mapped[int | None]
    expected_net: Mapped[float | None]
    flags: Mapped[dict[str, Any]] = mapped_column(JSON)
    rationale: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)


class Decision(Base):
    """My Take/Skip on a card. Append-only; the latest row per recommendation wins."""

    __tablename__ = "decisions"
    __table_args__ = (CheckConstraint(_one_of("action", DECISION_ACTIONS), name="action"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"), index=True)
    action: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


APPEND_ONLY_MODELS: tuple[type[Base], ...] = (
    Snapshot, DataAudit, ParamSet, Recommendation, Decision
)


class AppendOnlyError(RuntimeError):
    """An update or delete was attempted on an append-only table."""


@event.listens_for(Session, "before_flush")
def _refuse_append_only_changes(session: Session, _ctx: Any, _instances: Any) -> None:
    # ORM-level guard for a clear Python error early; the DB triggers are the backstop for
    # raw SQL.
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY_MODELS):
            raise AppendOnlyError(f"{obj.__tablename__} is append-only: delete refused")
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY_MODELS) and session.is_modified(obj):
            raise AppendOnlyError(f"{obj.__tablename__} is append-only: update refused")
