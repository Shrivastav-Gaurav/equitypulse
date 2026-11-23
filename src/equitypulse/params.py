"""Versioned parameter file: load, validate, hash, register.

Every scan, feature row and backtest records the params version; `param_sets` keeps each
version's exact text, append-only. The hash is over the parsed values, not the file bytes, so
a comment edit is not a new parameter set but any value change is.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from equitypulse.db.models import ParamSet

DEFAULT_PARAMS_PATH = Path("config/params/v1.yaml")

Horizon = Literal["1m", "3m", "6m", "10d"]
ScoredFactor = Literal["momentum", "volume_delivery", "reported_growth", "quality", "valuation"]
PriorFactor = Literal[
    "momentum", "volume_delivery", "earnings_momentum", "quality", "valuation", "flows"
]
# Weights are written to 1 decimal place and rounded so each horizon sums to exactly 100
# (10d momentum: exact 58.06, written 58.0), so allow 0.1 point; a swapped or mistyped
# weight is still caught.
WEIGHT_TOLERANCE = 0.1


class ParamsError(ValueError):
    """The parameter file is invalid or conflicts with a registered version."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Horizons(_Strict):
    offered: list[Horizon]
    default: Horizon
    research_only: list[Horizon]
    trading_days: dict[Horizon, int]


class Factors(_Strict):
    scored: list[ScoredFactor]
    not_yet_scored: list[str]
    reported_growth_share_of_earnings: float = Field(ge=0, le=1)
    prior_weights: dict[Horizon, dict[PriorFactor, float]]
    weights: dict[Horizon, dict[ScoredFactor, float]]
    min_sector_size: int = Field(ge=2)
    max_weight_change_per_month: float = Field(ge=0)


class Universe(_Strict):
    index: str
    series: list[str]
    exclude_surveillance: list[str]
    market_cap_floor_cr: float = Field(gt=0)
    market_cap_default_cr: float = Field(gt=0)
    min_median_traded_value_cr: float = Field(gt=0)
    min_price: float = Field(gt=0)
    min_history_trading_days: int = Field(gt=0)
    price_stale_trading_days: int = Field(ge=0)
    exclude_at_red_flags: int = Field(ge=1)
    exclude_at_operator_flags: int = Field(ge=1)


class Regime(_Strict):
    index: str
    ma_days: int = Field(gt=0)
    vix_above: float = Field(gt=0)
    blocked_horizons: list[Horizon]
    halve_risk_horizons: list[Horizon]


class Entry(_Strict):
    pullback_ma_days: int | None = None
    breakout_retest_within_atr: float | None = None


class Stop(_Strict):
    atr: float = Field(gt=0)
    structure: Literal["swing_low", "moving_average"]
    structure_days: int = Field(gt=0)


class TimeStopExtension(_Strict):
    to_days: int = Field(gt=0)
    while_above_ma_days: int = Field(gt=0)


class Trail(_Strict):
    breakeven_at_atr: float | None = None
    trail_atr: float | None = None
    after_atr: float | None = None
    below_ma_days: int | None = None
    weekly_closes: bool = False


class HorizonPlan(_Strict):
    entry: Entry
    stop: Stop
    target_atr: float = Field(gt=0)
    time_stop_days: int = Field(gt=0)
    time_stop_extension: TimeStopExtension | None = None
    trail: Trail


class TradePlan(_Strict):
    atr_days: int = Field(gt=0)
    min_reward_risk: float = Field(ge=1)
    entry_window_trading_days: int = Field(gt=0)
    skip_gap_above_entry_atr: float = Field(gt=0)
    target_cap: Literal["nearest_resistance"]
    long_term_conversion: bool
    horizons: dict[Horizon, HorizonPlan]


class KillSwitch(_Strict):
    drawdown: float = Field(gt=0, le=1)
    consecutive_stopouts: int = Field(gt=0)


class Risk(_Strict):
    risk_per_trade: float = Field(gt=0, le=0.05)
    max_position: float = Field(gt=0, le=1)
    max_adv_fraction: float = Field(gt=0, le=1)
    adv_days: int = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    max_sector_capital: float = Field(gt=0, le=1)
    max_sector_positions: int = Field(gt=0)
    max_new_per_scan: int = Field(gt=0)
    max_per_sector_cross_market: int = Field(gt=0)
    correlation_flag_above: float = Field(gt=0, le=1)
    kill_switch: KillSwitch


class CostsTax(_Strict):
    round_trip_cost_liquid: float = Field(ge=0, le=0.05)
    stcg_rate: float = Field(ge=0, le=1)
    ltcg_rate: float = Field(ge=0, le=1)
    ltcg_exemption_inr: float = Field(ge=0)
    ltcg_after_months: int = Field(gt=0)


class Calibration(_Strict):
    min_n: int = Field(gt=0)


class Ranking(_Strict):
    picks: int = Field(gt=0)
    watch_list: int = Field(ge=0)
    tie_break: Literal["liquidity"]


class LongTermFlag(_Strict):
    piotroski_min: int = Field(ge=0, le=9)
    roce_5y_min: float
    lender_roa_min: float
    beneish_max: float
    above_ma_days: int = Field(gt=0)


class Backtest(_Strict):
    holdout_months: int = Field(gt=0)
    early_exit_composite_pct_below: float = Field(ge=0, le=100)


class Params(_Strict):
    version: str = Field(pattern=r"^v\d+$")
    horizons: Horizons
    factors: Factors
    universe: Universe
    regime: Regime
    trade_plan: TradePlan
    risk: Risk
    costs_tax: CostsTax
    calibration: Calibration
    ranking: Ranking
    long_term_flag: LongTermFlag
    backtest: Backtest

    @model_validator(mode="after")
    def _consistent(self) -> "Params":
        h = self.horizons
        all_h = set(h.offered) | set(h.research_only)
        if set(h.offered) & set(h.research_only):
            raise ValueError("a horizon cannot be both offered and research-only")
        if h.default not in h.offered:
            raise ValueError(f"default horizon {h.default} is not offered")
        for name, keys in {
            "horizons.trading_days": h.trading_days,
            "factors.prior_weights": self.factors.prior_weights,
            "factors.weights": self.factors.weights,
            "trade_plan.horizons": self.trade_plan.horizons,
        }.items():
            if set(keys) != all_h:
                raise ValueError(f"{name} must cover exactly {sorted(all_h)}")
        for hz, plan in self.trade_plan.horizons.items():
            if plan.time_stop_days != h.trading_days[hz]:
                raise ValueError(f"{hz}: time stop {plan.time_stop_days} != trading_days")
            ext = plan.time_stop_extension
            if ext is not None and ext.to_days <= plan.time_stop_days:
                raise ValueError(f"{hz}: time-stop extension must be longer than the time stop")
        for field in ("blocked_horizons", "halve_risk_horizons"):
            if not set(getattr(self.regime, field)) <= set(h.offered):
                raise ValueError(f"regime.{field} must be offered horizons")
        if self.universe.market_cap_default_cr < self.universe.market_cap_floor_cr:
            raise ValueError("market cap default is below the floor")
        _check_weights(self.factors)
        return self

    @property
    def hash(self) -> str:
        """Over the values only (not the version name), so a renamed copy is recognised."""
        values = self.model_dump(mode="json", exclude={"version"})
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_weights(
    prior: dict[str, float], scored: list[str], growth_share: float
) -> dict[str, float]:
    """Phase 1a weights from PRD priors: reported growth takes `growth_share` of earnings
    momentum; everything not scored is spread proportionally over the scored factors."""
    base: dict[str, float] = {}
    for factor in scored:
        if factor == "reported_growth":
            base[factor] = prior["earnings_momentum"] * growth_share
        else:
            base[factor] = prior[factor]
    total = sum(base.values())
    if total <= 0:
        raise ValueError("no scored weight to spread")
    return {f: 100.0 * w / total for f, w in base.items()}


def _check_weights(factors: Factors) -> None:
    for hz, prior in factors.prior_weights.items():
        if abs(sum(prior.values()) - 100) > 1e-9:
            raise ValueError(f"prior weights for {hz} sum to {sum(prior.values())}, not 100")
        weights = factors.weights[hz]
        if set(weights) != set(factors.scored):
            raise ValueError(f"weights for {hz} must cover exactly the scored factors")
        if abs(sum(weights.values()) - 100) > WEIGHT_TOLERANCE:
            raise ValueError(f"weights for {hz} sum to {sum(weights.values()):.2f}, not 100")
        derived = derive_weights(
            prior, list(factors.scored), factors.reported_growth_share_of_earnings
        )
        for f, w in weights.items():
            if abs(w - derived[f]) > WEIGHT_TOLERANCE:
                raise ValueError(
                    f"weight {hz}.{f} = {w} does not follow from the priors ({derived[f]:.2f})"
                )


class ParamsFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    params: Params
    text: str

    @property
    def version(self) -> str:
        return self.params.version

    @property
    def hash(self) -> str:
        return self.params.hash


def load_params(path: Path = DEFAULT_PARAMS_PATH) -> ParamsFile:
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
        params = Params.model_validate(raw)
    except (yaml.YAMLError, ValueError) as exc:
        raise ParamsError(f"{path}: {exc}") from exc
    return ParamsFile(params=params, text=text)


def register_params(session: Session, pf: ParamsFile) -> ParamSet:
    """Store this version once. A registered version whose values changed is refused: edit a
    copy as a new version instead. Does not commit."""
    existing = session.get(ParamSet, pf.version)
    if existing is not None:
        if existing.hash != pf.hash:
            raise ParamsError(
                f"params {pf.version} is already registered with different values; "
                "save the change as a new version"
            )
        return existing
    clash = session.query(ParamSet).filter(ParamSet.hash == pf.hash).one_or_none()
    if clash is not None:
        raise ParamsError(f"these values are already registered as {clash.version}")
    row = ParamSet(version=pf.version, hash=pf.hash, yaml=pf.text)
    session.add(row)
    session.flush()
    return row
