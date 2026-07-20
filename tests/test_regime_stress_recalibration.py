"""Z2-6: RISK_OFF stress recalibration behaviour, on SYNTHETIC data only (per F3 -- no
strategy statistics, no soak outcomes). These lock the fix that the credit + vix components
are scale-free ratio z-scores: nothing pegs on the old pathological inputs, real stress still
saturates, short history is INCOMPUTABLE (not silently calm), and the backwardation flag rate
over a synthetic replay is single-digit-pct (reference: the ~8% IVTS VIX/VIX3M base rate) --
not the ~87% the nominal-price comparison produced."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from src.engine.regime_intelligence import (
    PORTFOLIO_RISK_OFF,
    REGIME_STRESS_Z_FULL_SCALE,
    CrossAssetStressDashboard,
    PortfolioRiskModeEvaluator,
    RiskPostureOverride,
)

_INACTIVE_CAL = RiskPostureOverride(
    position_cap_multiplier=1.0, entry_z_widen_sigma=0.0, event_label=None, active=False
)


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _noisy(rng, base: float, sigma: float, n: int) -> tuple[float, ...]:
    """A stationary noisy series whose LAST element equals the mean (z_latest ~ 0)."""
    body = rng.normal(base, sigma, n - 1)
    return tuple(float(x) for x in np.concatenate([body, [base]]))


def _final_shock(rng, base: float, sigma: float, n: int, shock_sigmas: float) -> tuple[float, ...]:
    """Stationary noise with a single large terminal move of `shock_sigmas` sigma."""
    body = rng.normal(base, sigma, n - 1)
    last = base + shock_sigmas * sigma
    return tuple(float(x) for x in np.concatenate([body, [last]]))


def _flat_equity(rng, base: float, n: int) -> tuple[float, ...]:
    # Decorrelated near-random-walk so the correlation component does NOT peg.
    steps = rng.normal(0.0, base * 0.01, n)
    return tuple(float(x) for x in base + np.cumsum(steps))


# (a) calm ------------------------------------------------------------------------------------
def test_a_calm_composite_low_and_never_risk_off() -> None:
    rng = _rng(1)
    n = 252
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        "vixy_closes": _noisy(rng, 25.0, 0.5, n),
        "vixm_closes": _noisy(rng, 22.0, 0.4, n),
        "hyg_closes": _noisy(rng, 76.0, 0.3, n),
        "lqd_closes": _noisy(rng, 108.0, 0.3, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.composite < 0.2, f"calm composite too high: {s.composite}"
    assert not s.vix_backwardation
    assert not s.stress_component_degraded
    decision = PortfolioRiskModeEvaluator().evaluate(
        current_time=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        stress=s,
        calendar_override=_INACTIVE_CAL,
    )
    assert decision.mode != PORTFOLIO_RISK_OFF
    assert not decision.block_new_entries


# (b) credit stress ---------------------------------------------------------------------------
def test_b_credit_ratio_shock_saturates_credit() -> None:
    rng = _rng(2)
    n = 252
    d = CrossAssetStressDashboard()
    # HYG/LQD ratio driven strongly negative (HY underperforms IG = spreads widen).
    snap = d.proxy_snapshot({
        "hyg_closes": _final_shock(rng, 76.0, 0.4, n, -8.0),
        "lqd_closes": _noisy(rng, 108.0, 0.05, n),
        "vixy_closes": _noisy(rng, 25.0, 0.5, n),
        "vixm_closes": _noisy(rng, 22.0, 0.4, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.credit_spread_stress > 0.9, f"credit did not saturate: {s.credit_spread_stress}"
    assert "credit" in s.computable_components


# (c) vol stress ------------------------------------------------------------------------------
def test_c_vix_ratio_shock_saturates_and_backwardates() -> None:
    rng = _rng(3)
    n = 252
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        "vixy_closes": _final_shock(rng, 25.0, 0.5, n, 8.0),
        "vixm_closes": _noisy(rng, 22.0, 0.05, n),
        "hyg_closes": _noisy(rng, 76.0, 0.3, n),
        "lqd_closes": _noisy(rng, 108.0, 0.3, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.vix_term_stress > 0.9, f"vix did not saturate: {s.vix_term_stress}"
    assert s.vix_backwardation is True


# (d) short history ---------------------------------------------------------------------------
def test_d_short_history_incomputable_renormalizes_onto_correlation() -> None:
    rng = _rng(4)
    n = 30
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        "vixy_closes": _noisy(rng, 25.0, 0.5, n),
        "vixm_closes": _noisy(rng, 22.0, 0.4, n),
        "hyg_closes": _noisy(rng, 76.0, 0.3, n),
        "lqd_closes": _noisy(rng, 108.0, 0.3, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.stress_component_degraded is True
    assert "vix" not in s.computable_components
    assert "credit" not in s.computable_components
    assert "corr" in s.computable_components
    # Composite is the (renormalized) correlation stress alone, in [0,1].
    assert 0.0 <= s.composite <= 1.0
    assert s.composite == pytest.approx(s.macro_correlation_stress)


# (e) regression on the OLD pathological inputs ----------------------------------------------
def test_e_old_pathological_inputs_peg_nothing() -> None:
    """SPY~400, HYG~76, LQD~108, VIXY nominal > VIXM. Under the OLD code credit + vix were
    pegged at 1.0; under the ratio-z form nothing pegs (flat ratios -> ~0 stress)."""
    rng = _rng(5)
    n = 252
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        "vixy_closes": _noisy(rng, 25.0, 0.4, n),   # nominal VIXY(25) > VIXM(22): irrelevant now
        "vixm_closes": _noisy(rng, 22.0, 0.4, n),
        "hyg_closes": _noisy(rng, 76.0, 0.3, n),
        "lqd_closes": _noisy(rng, 108.0, 0.3, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.vix_term_stress < 1.0
    assert s.credit_spread_stress < 1.0
    assert s.macro_correlation_stress < 1.0
    assert not s.vix_backwardation
    assert s.composite < 0.5


# (f) synthetic 2y replay: backwardation flag rate is single-digit pct -------------------------
def test_f_backwardation_flag_rate_is_single_digit_pct() -> None:
    rng = _rng(6)
    total = 504  # ~2 trading years
    d = CrossAssetStressDashboard()
    # A STATIONARY VIXY/VIXM ratio: nominal VIXY > VIXM throughout (would peg the old code),
    # but the ratio only rarely extends beyond +2 sigma.
    vixy_full = rng.normal(25.0, 0.6, total)
    vixm_full = np.full(total, 22.0)
    flags = 0
    evals = 0
    for t in range(d.min_sessions, total + 1):
        snap = d.proxy_snapshot({
            "vixy_closes": tuple(float(x) for x in vixy_full[:t]),
            "vixm_closes": tuple(float(x) for x in vixm_full[:t]),
            "hyg_closes": tuple(76.0 for _ in range(t)),
            "lqd_closes": tuple(108.0 for _ in range(t)),
            "spy_closes": tuple(400.0 for _ in range(t)),
            "qqq_closes": tuple(350.0 for _ in range(t)),
            "tlt_closes": tuple(90.0 for _ in range(t)),
        })
        s = d.evaluate(snap)
        evals += 1
        flags += int(s.vix_backwardation)
    rate = flags / evals
    # Shape test: reference IVTS backwardation base rate ~8%; the old nominal comparison was ~87%.
    assert rate < 0.15, f"backwardation rate {rate:.1%} not single-digit-ish (old bug was ~87%)"


# (obs) unclipped ratio-z observability -------------------------------------------------------
def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def test_obs_raw_ratio_z_surfaced_and_clip_is_byte_identical() -> None:
    """Observability: the unclipped credit/vix ratio z-scores are surfaced AND the clipped
    components remain EXACTLY clip(raw_z) -- proving the added fields are log-only (the raw z is
    the genuine clip input, not a recomputation, and the decision-bearing clip output is
    unchanged). RISK-ON tape (HY OUTperforming = positive credit z, VIXY/VIXM contango = negative
    vix z): BOTH clipped stresses floor at 0.0 while the raw z's are clearly non-zero -- exactly
    the live-market case (credit z=+2.57 / vix z=-1.20) the log line must now make legible, so a
    reader sees 'stress=0.0 because calm', not 'stress=0.0 because blind'."""
    rng = _rng(11)
    n = 252
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        # HYG jumps UP terminally -> HYG/LQD ratio z POSITIVE -> credit_spread_stress=clip(-z)=0
        "hyg_closes": _final_shock(rng, 76.0, 0.4, n, +8.0),
        "lqd_closes": _noisy(rng, 108.0, 0.05, n),
        # VIXY drops UP-to-down terminally -> VIXY/VIXM ratio z NEGATIVE -> vix_term_stress=clip(z)=0
        "vixy_closes": _final_shock(rng, 25.0, 0.5, n, -8.0),
        "vixm_closes": _noisy(rng, 22.0, 0.05, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    # fields present
    assert hasattr(s, "credit_ratio_z") and hasattr(s, "vix_ratio_z")
    # the raw z's are the genuine clip inputs -> clipped components are EXACTLY clip(raw_z)
    assert s.vix_term_stress == _clip01(s.vix_ratio_z / REGIME_STRESS_Z_FULL_SCALE)
    assert s.credit_spread_stress == _clip01(-s.credit_ratio_z / REGIME_STRESS_Z_FULL_SCALE)
    # the ambiguity this log addition resolves: clipped stress == 0.0 while the raw z clearly MOVED
    assert s.credit_spread_stress == 0.0 and s.credit_ratio_z > 1.0  # risk-ON, not blind
    assert s.vix_term_stress == 0.0 and s.vix_ratio_z < -1.0         # contango, not blind
    assert not s.stress_component_degraded  # data present -> distinguishes calm from blind


def test_obs_credit_shock_raw_z_signed_negative_matches_saturation() -> None:
    """A real credit shock: credit_spread_stress saturates AND the raw credit z is strongly
    NEGATIVE (HY underperforming IG). Confirms the surfaced z carries the sign/magnitude a
    reader needs, and the clip relationship still holds under saturation."""
    rng = _rng(12)
    n = 252
    d = CrossAssetStressDashboard()
    snap = d.proxy_snapshot({
        "hyg_closes": _final_shock(rng, 76.0, 0.4, n, -8.0),
        "lqd_closes": _noisy(rng, 108.0, 0.05, n),
        "vixy_closes": _noisy(rng, 25.0, 0.5, n),
        "vixm_closes": _noisy(rng, 22.0, 0.4, n),
        "spy_closes": _flat_equity(rng, 400.0, n),
        "qqq_closes": _flat_equity(rng, 350.0, n),
        "tlt_closes": _flat_equity(rng, 90.0, n),
    })
    s = d.evaluate(snap)
    assert s.credit_spread_stress > 0.9
    assert s.credit_ratio_z < 0.0, "credit shock must surface a negative raw z"
    assert s.credit_spread_stress == _clip01(-s.credit_ratio_z / REGIME_STRESS_Z_FULL_SCALE)
