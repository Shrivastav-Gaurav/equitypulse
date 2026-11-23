"""T16: the versioned parameter file and its registry."""

from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from equitypulse.db.models import ParamSet
from equitypulse.params import (
    DEFAULT_PARAMS_PATH,
    ParamsError,
    derive_weights,
    load_params,
    register_params,
)

# The spec's Phase 1a weight table (docs/phase1a_spec.md), copied by hand.
SPEC_WEIGHTS = {
    "1m": {"momentum": 46.7, "volume_delivery": 20.0, "reported_growth": 13.3, "quality": 13.3,
           "valuation": 6.7},
    "3m": {"momentum": 34.5, "volume_delivery": 6.9, "reported_growth": 17.2, "quality": 27.6,
           "valuation": 13.8},
    "6m": {"momentum": 27.6, "volume_delivery": 0.0, "reported_growth": 17.2, "quality": 34.5,
           "valuation": 20.7},
    "10d": {"momentum": 58.0, "volume_delivery": 25.8, "reported_growth": 9.7, "quality": 6.5,
            "valuation": 0.0},
}


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(DEFAULT_PARAMS_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# ── the v1 file ───────────────────────────────────────────────────────────────


def test_v1_loads_and_matches_the_spec_weights() -> None:
    p = load_params().params
    assert p.version == "v1"
    assert p.factors.weights == SPEC_WEIGHTS
    for hz, w in p.factors.weights.items():
        assert sum(w.values()) == pytest.approx(100, abs=0.05), hz


def test_v1_horizons_3m_primary_6m_offered_10d_research_only() -> None:
    h = load_params().params.horizons
    assert h.default == "3m"
    assert h.offered == ["1m", "3m", "6m"]
    assert h.research_only == ["10d"]


def test_v1_trade_plan_rules_for_every_horizon() -> None:
    plans = load_params().params.trade_plan.horizons
    assert {hz: (p.stop.atr, p.target_atr, p.time_stop_days) for hz, p in plans.items()} == {
        "1m": (2.0, 4.5, 21), "3m": (2.5, 7.0, 63), "6m": (3.5, 10.0, 126), "10d": (1.5, 3.0, 10),
    }
    six = plans["6m"]
    assert six.time_stop_extension is not None
    assert (six.time_stop_extension.to_days, six.time_stop_extension.while_above_ma_days) == (
        189, 50,
    )
    assert six.stop.structure == "moving_average" and six.stop.structure_days == 100
    assert six.trail.weekly_closes is True
    assert load_params().params.trade_plan.long_term_conversion is False


def test_regime_gate_blocks_1m_and_halves_3m_6m() -> None:
    r = load_params().params.regime
    assert r.blocked_horizons == ["1m"] and r.halve_risk_horizons == ["3m", "6m"]


def test_weights_follow_from_the_prd_priors() -> None:
    f = load_params().params.factors
    for hz, prior in f.prior_weights.items():
        derived = derive_weights(prior, list(f.scored), f.reported_growth_share_of_earnings)
        for factor, w in f.weights[hz].items():
            assert w == pytest.approx(derived[factor], abs=0.1), (hz, factor)


def test_without_reported_growth_3m_matches_the_spec_fallback() -> None:
    f = load_params().params.factors
    w = derive_weights(f.prior_weights["3m"], ["momentum", "volume_delivery", "quality",
                                               "valuation"], 0.5)
    assert {k: round(v, 1) for k, v in w.items()} == {
        "momentum": 41.7, "volume_delivery": 8.3, "quality": 33.3, "valuation": 16.7,
    }


# ── hashing ───────────────────────────────────────────────────────────────────


def test_hash_is_stable() -> None:
    assert load_params().hash == load_params().hash


def test_changing_a_value_changes_the_hash(tmp_path: Path, raw: dict) -> None:
    raw["risk"]["risk_per_trade"] = 0.005
    assert load_params(_write(tmp_path, raw)).hash != load_params().hash


def test_a_comment_edit_does_not_change_the_hash(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(DEFAULT_PARAMS_PATH.read_text(encoding="utf-8") + "\n# a note\n",
                    encoding="utf-8")
    assert load_params(path).hash == load_params().hash


# ── validation ────────────────────────────────────────────────────────────────


def test_unknown_key_is_rejected(tmp_path: Path, raw: dict) -> None:
    raw["risk"]["risk_per_trad"] = 0.01
    with pytest.raises(ParamsError, match="risk_per_trad"):
        load_params(_write(tmp_path, raw))


def test_weights_not_summing_to_100_are_rejected(tmp_path: Path, raw: dict) -> None:
    raw["factors"]["weights"]["3m"]["momentum"] = 44.5
    with pytest.raises(ParamsError, match="sum"):
        load_params(_write(tmp_path, raw))


def test_swapped_weights_are_caught_by_the_prior_check(tmp_path: Path, raw: dict) -> None:
    w = raw["factors"]["weights"]["3m"]
    w["quality"], w["momentum"] = w["momentum"], w["quality"]  # still sums to 100
    with pytest.raises(ParamsError, match="does not follow from the priors"):
        load_params(_write(tmp_path, raw))


def test_default_horizon_must_be_offered(tmp_path: Path, raw: dict) -> None:
    raw["horizons"]["default"] = "10d"
    with pytest.raises(ParamsError, match="not offered"):
        load_params(_write(tmp_path, raw))


def test_research_horizon_cannot_be_offered(tmp_path: Path, raw: dict) -> None:
    raw["horizons"]["offered"].append("10d")
    with pytest.raises(ParamsError, match="both offered and research-only"):
        load_params(_write(tmp_path, raw))


def test_every_horizon_needs_a_trade_plan(tmp_path: Path, raw: dict) -> None:
    del raw["trade_plan"]["horizons"]["6m"]
    with pytest.raises(ParamsError, match="trade_plan.horizons"):
        load_params(_write(tmp_path, raw))


def test_time_stop_must_match_the_horizon_length(tmp_path: Path, raw: dict) -> None:
    raw["trade_plan"]["horizons"]["3m"]["time_stop_days"] = 60
    with pytest.raises(ParamsError, match="time stop"):
        load_params(_write(tmp_path, raw))


def test_market_cap_default_cannot_sit_below_the_floor(tmp_path: Path, raw: dict) -> None:
    raw["universe"]["market_cap_default_cr"] = 1500
    with pytest.raises(ParamsError, match="floor"):
        load_params(_write(tmp_path, raw))


# ── registry ──────────────────────────────────────────────────────────────────


def test_register_stores_the_version_once(db_session: Session) -> None:
    pf = load_params()
    row = register_params(db_session, pf)
    db_session.commit()
    assert (row.version, row.hash) == ("v1", pf.hash)
    assert row.yaml == DEFAULT_PARAMS_PATH.read_text(encoding="utf-8")
    register_params(db_session, pf)
    db_session.commit()
    assert db_session.query(ParamSet).count() == 1


def test_same_version_with_changed_values_is_refused(
    db_session: Session, tmp_path: Path, raw: dict
) -> None:
    register_params(db_session, load_params())
    db_session.commit()
    raw["risk"]["risk_per_trade"] = 0.005
    with pytest.raises(ParamsError, match="new version"):
        register_params(db_session, load_params(_write(tmp_path, raw)))


def test_same_values_under_a_new_version_name_are_refused(
    db_session: Session, tmp_path: Path, raw: dict
) -> None:
    register_params(db_session, load_params())
    db_session.commit()
    raw["version"] = "v2"
    with pytest.raises(ParamsError, match="already registered as v1"):
        register_params(db_session, load_params(_write(tmp_path, raw)))
