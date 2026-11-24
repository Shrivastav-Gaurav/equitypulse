"""T24: momentum and volume/delivery inputs, sector percentiles, the demerger rule."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from equitypulse.factors.cross_section import cross_section, security_inputs
from equitypulse.factors.demerger import spans_demerger
from equitypulse.factors.momentum import momentum_inputs
from equitypulse.factors.ranking import percentile, sector_percentiles
from equitypulse.factors.volume import volume_inputs

START = dt.date(2020, 1, 1)


def _frame(closes, volumes=None, delivery=None) -> pd.DataFrame:
    n = len(closes)
    dates = [START + dt.timedelta(days=i) for i in range(n)]
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "date": dates, "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": volumes if volumes is not None else [1000.0] * n,
        "delivery_pct": delivery if delivery is not None else [0.4] * n,
    })


# ── percentiles ───────────────────────────────────────────────────────────────


def test_percentile_spans_0_to_100_with_shared_ties_and_nan_kept() -> None:
    p = percentile(pd.Series([3.0, 1.0, 2.0, 2.0, float("nan")]))
    assert p.tolist()[:4] == [100.0, 0.0, 50.0, 50.0]
    assert np.isnan(p.iloc[4])
    assert percentile(pd.Series([7.0])).tolist() == [50.0]


def test_sector_percentiles_rank_within_sector_and_small_sectors_use_the_universe() -> None:
    values = pd.Series(list(range(10)) + [100.0, -100.0], index=[f"s{i}" for i in range(12)])
    sectors = pd.Series(["A"] * 10 + ["TINY", "TINY"], index=values.index)
    p = sector_percentiles(values, sectors, min_size=8)
    assert p["s0"] == 0.0 and p["s9"] == 100.0  # ranked within A
    assert p["s10"] == 100.0 and p["s11"] == 0.0  # TINY ranked against everyone
    assert p["s5"] == pytest.approx(5 / 9 * 100)


# ── momentum ──────────────────────────────────────────────────────────────────


def test_momentum_inputs_on_a_steady_uptrend() -> None:
    closes = [100 * 1.001**i for i in range(300)]
    m = momentum_inputs(_frame(closes))
    last = m.iloc[-1]
    assert last["ret_1m"] == pytest.approx(1.001**21 - 1)
    assert last["ret_6m"] == pytest.approx(1.001**126 - 1)
    assert last["dist_52w_high"] == pytest.approx(0.0)  # at a new high
    assert last["trend_template"] == 4.0  # price > 50 > 150 > 200, 200 rising
    assert last["weinstein_stage"] == 2.0
    assert np.isnan(m["trend_template"].iloc[150])  # not enough history yet


def test_breakout_needs_a_new_20_day_high_on_volume() -> None:
    closes = [100.0] * 40 + [110.0, 111.0]
    volumes = [1000.0] * 40 + [2000.0, 1000.0]
    m = momentum_inputs(_frame(closes, volumes))
    assert m["breakout"].iloc[40] == 1.0  # new high, 2x volume
    assert m["breakout"].iloc[41] == 0.0  # new high but ordinary volume
    assert m["breakout"].iloc[30] == 0.0


def test_a_downtrend_is_weinstein_stage_4_with_an_empty_template() -> None:
    closes = [100 * 0.999**i for i in range(300)]
    last = momentum_inputs(_frame(closes)).iloc[-1]
    assert last["weinstein_stage"] == 4.0 and last["trend_template"] == 0.0
    assert last["dist_52w_high"] < -0.2


def test_inputs_for_a_date_never_change_when_later_bars_arrive() -> None:
    rng = np.random.default_rng(7)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.02, 400))
    volumes = rng.integers(500, 5000, 400).astype(float)
    full = _frame(closes, volumes)
    shocked = full.copy()
    shocked.loc[300:, "close"] *= 0.3  # a crash after D
    shocked.loc[300:, "high"] *= 0.3
    d = full["date"].iloc[299]
    a = security_inputs(full[full["date"] <= d]).loc[d]
    b = security_inputs(shocked).loc[d]
    pd.testing.assert_series_equal(a, b, check_names=False)


# ── volume and delivery ───────────────────────────────────────────────────────


def test_volume_and_delivery_ratios() -> None:
    volumes = [1000.0] * 55 + [3000.0] * 5
    delivery = [0.40] * 55 + [0.60] * 5
    v = volume_inputs(_frame([100.0] * 60, volumes, delivery)).iloc[-1]
    assert v["volume_ratio"] == pytest.approx(3000 / ((15 * 1000 + 5 * 3000) / 20))
    assert v["delivery_ratio"] == pytest.approx(0.60 / ((55 * 0.40 + 5 * 0.60) / 60))


def test_missing_delivery_leaves_the_delivery_ratio_empty() -> None:
    v = volume_inputs(_frame([100.0] * 70, delivery=[float("nan")] * 70)).iloc[-1]
    assert np.isnan(v["delivery_ratio"]) and v["volume_ratio"] == pytest.approx(1.0)


# ── the demerger rule ─────────────────────────────────────────────────────────


def test_demerger_window_blocks_momentum_and_plans_until_it_clears() -> None:
    df = _frame([100.0] * 400)
    ex = df["date"].iloc[100]
    inputs = security_inputs(df, [ex])
    dates = inputs.index
    assert not inputs.loc[dates[99], "momentum_blocked"]  # before the ex-date
    assert inputs.loc[dates[100], "momentum_blocked"]
    assert inputs.loc[dates[351], "momentum_blocked"]  # still inside 252 bars
    assert not inputs.loc[dates[352], "momentum_blocked"]  # window clear
    assert inputs.loc[dates[114], "plan_blocked"] and not inputs.loc[dates[115], "plan_blocked"]


def test_spans_demerger_handles_no_ex_dates_and_ex_dates_outside_the_data() -> None:
    dates = pd.Index([START + dt.timedelta(days=i) for i in range(10)])
    assert not spans_demerger(dates, [], 5).any()
    assert not spans_demerger(dates, [START + dt.timedelta(days=50)], 5).any()


# ── cross-section ─────────────────────────────────────────────────────────────


def _rows(n: int, sector: str = "IT") -> tuple[pd.DataFrame, pd.Series]:
    idx = [f"INE{i:09d}" for i in range(n)]
    base = pd.Series(range(n), index=idx, dtype=float)
    rows = pd.DataFrame({
        "ret_1m": base, "ret_3m": base, "ret_6m": base, "dist_52w_high": base,
        "trend_template": base, "breakout": 0.0, "volume_ratio": base,
        "delivery_ratio": base, "momentum_blocked": False, "plan_blocked": False,
    }, index=idx)
    return rows, pd.Series(sector, index=idx)


def test_cross_section_ranks_the_strongest_stock_highest() -> None:
    rows, sectors = _rows(10)
    out = cross_section(rows, sectors)
    assert out["pct_momentum"].iloc[-1] == 100.0 and out["pct_momentum"].iloc[0] == 0.0
    assert out["pct_volume_delivery"].iloc[-1] == 100.0


def test_a_blocked_stock_has_no_momentum_but_keeps_its_volume_factor() -> None:
    rows, sectors = _rows(10)
    rows.loc[rows.index[9], "momentum_blocked"] = True
    out = cross_section(rows, sectors)
    assert np.isnan(out["pct_momentum"].iloc[9])
    assert out["pct_momentum"].iloc[8] == 100.0  # ranks among the rest
    assert out["pct_volume_delivery"].iloc[9] == 100.0


def test_momentum_needs_four_inputs() -> None:
    rows, sectors = _rows(10)
    rows.loc[rows.index[5], ["ret_1m", "ret_3m", "ret_6m"]] = float("nan")
    out = cross_section(rows, sectors)
    assert np.isnan(out["pct_momentum"].iloc[5])  # 3 of 6 inputs left
