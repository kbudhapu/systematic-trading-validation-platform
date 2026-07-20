"""G2.4 PortfolioBrain cluster layer (PAD v1.0). Synthetic calibration with ground
truth by construction: equal-risk budgeting, admission gate, cluster convergence,
freed-budget-to-cash, and cold-start caps."""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import (
    LegInput, PortfolioConfig, admission_decision, allocate, blend_sharpe,
    convergence_watch,
)

CFG = PortfolioConfig()


def _leg(name, cluster, seed, *, order=0, state="ACTIVE", n=120, mean=0.003, vol=0.02):
    r = np.random.default_rng(seed).normal(mean, vol, n)
    return LegInput(name, cluster, r, oos_vol=vol, admission_order=order, state=state)


def test_three_uncorrelated_legs_equal_risk_and_blend_beats_best_single() -> None:
    """(a) three ~uncorrelated legs, each its own cluster -> equal-risk budgets and
    a blend Sharpe above the best single leg."""
    legs = [_leg("A", "ATTN", 1), _leg("B", "TREND", 2), _leg("C", "RV", 3)]
    res = allocate(legs, CFG)
    ws = [res.weights[l.leg_id] for l in legs]
    assert max(ws) - min(ws) < 1e-6, f"budgets should be ~equal: {ws}"
    blend = blend_sharpe(legs, res.weights)
    best_single = max(float(np.mean(l.returns) / np.std(l.returns, ddof=1)) for l in legs)
    assert blend > best_single, f"blend {blend:.3f} must beat best single {best_single:.3f}"
    # allocation log persists the inputs (corr/vol matrices) + cash share
    assert "corr_matrix" in res.log and "cluster_vol" in res.log and "cash_share" in res.log
    assert res.cash_share == res.log["cash_share"]


def test_correlated_candidate_admission_rejected() -> None:
    """(b) a candidate with rho ~ 0.5 vs an active leg is REJECTED (|rho| >= 0.35)."""
    base = np.random.default_rng(5).normal(0.003, 0.02, 120)
    z = np.random.default_rng(1).normal(0, 0.02, 120)
    cand_r = 0.5 * base + np.sqrt(0.75) * z             # sample rho ~ 0.52
    rho = float(np.corrcoef(cand_r, base)[0, 1])
    assert rho >= 0.35
    active = [LegInput("X", "ATTN", base, 0.02)]
    decision = admission_decision(LegInput("Y", "TREND", cand_r, 0.02), active, CFG)
    assert decision.verdict == "REJECT_CORR"


def test_short_overlap_deferred_not_waived() -> None:
    """(c) 40 weeks of overlap (< 52) -> DEFERRED, not waived."""
    active = [_leg("X", "ATTN", 5)]
    short = _leg("Z", "RV", 7, n=40)
    decision = admission_decision(short, active, CFG)
    assert decision.verdict == "DEFER_OVERLAP"


def test_planted_correlation_convergence_flags_junior_leg() -> None:
    """(d) two legs whose correlation ramps to ~0.85 in the tail -> the JUNIOR
    (later-admitted) leg is flagged for WATCH within the convergence window."""
    na = np.random.default_rng(8).normal(0, 0.02, 120)
    nb = np.random.default_rng(9).normal(0, 0.02, 120)
    a_r, b_r = na.copy(), nb.copy()
    b_r[-55:] = 0.85 * a_r[-55:] + np.sqrt(1 - 0.85 ** 2) * nb[-55:]
    a = LegInput("A", "ATTN", a_r, 0.02, admission_order=0)   # senior
    b = LegInput("B", "ATTN", b_r, 0.02, admission_order=1)   # junior
    alert = convergence_watch(a, b, CFG)
    assert alert is not None
    assert alert.junior_leg == "B" and alert.consecutive_weeks >= 4


def test_stable_pair_no_convergence_alert() -> None:
    a = _leg("A", "ATTN", 30, order=0)
    b = _leg("B", "TREND", 31, order=1)   # independent
    assert convergence_watch(a, b, CFG) is None


def test_safe_mode_leg_budget_goes_to_cash_not_sibling() -> None:
    """(e) a SHORTVOL leg dropping to SAFE_MODE -> its budget goes to CASH; its
    cluster sibling's weight is UNCHANGED (never auto-redistributed intra-cluster)."""
    s1 = _leg("S1", "SHORTVOL", 10)
    s2 = _leg("S2", "SHORTVOL", 11)
    both = allocate([s1, s2], CFG)
    sibling_before = both.weights["S2"]
    s1.state = "SAFE_MODE"
    after = allocate([s1, s2], CFG)
    assert after.weights["S1"] == 0.0
    assert abs(after.weights["S2"] - sibling_before) < 1e-9, "sibling must not absorb freed budget"
    assert after.cash_share > both.cash_share, "freed budget must flow to cash"


def test_watch_leg_half_sized_remainder_to_cash() -> None:
    s1 = _leg("S1", "SHORTVOL", 10)
    s2 = _leg("S2", "SHORTVOL", 11)
    both = allocate([s1, s2], CFG)
    s1.state = "WATCH"
    after = allocate([s1, s2], CFG)
    assert abs(after.weights["S1"] - 0.5 * both.weights["S1"]) < 1e-9
    assert abs(after.weights["S2"] - both.weights["S2"]) < 1e-9


def test_cold_start_single_leg_caps_bind_cash_reported() -> None:
    """(f) a single ACTIVE leg -> capped at the 25% leg cap; the remaining 75%
    stays in cash and is reported."""
    res = allocate([_leg("O", "ATTN", 20)], CFG)
    assert abs(res.weights["O"] - 0.25) < 1e-9, "single leg capped at the 25% leg cap"
    assert abs(res.cash_share - 0.75) < 1e-9
    assert res.log["cash_share"] == res.cash_share


def test_shortvol_cluster_cap_binds_at_15pct() -> None:
    legs = [_leg("V1", "SHORTVOL", 40), _leg("V2", "SHORTVOL", 41), _leg("V3", "SHORTVOL", 42)]
    res = allocate(legs, CFG)
    total_sv = sum(res.weights[l.leg_id] for l in legs)
    assert total_sv <= 0.15 + 1e-9, f"SHORTVOL cluster capped at 15%, got {total_sv:.3f}"


def test_rebalance_step_cap_limits_swing() -> None:
    legs = [
        LegInput("A", "ATTN", np.random.default_rng(1).normal(0.003, 0.02, 120), 0.02, prior_weight=0.0),
        LegInput("B", "TREND", np.random.default_rng(2).normal(0.003, 0.02, 120), 0.02, prior_weight=0.0),
    ]
    res = allocate(legs, CFG, apply_step_cap=True)
    # from a 0.0 prior no leg may jump more than the 20% step cap in one rebalance
    assert all(w <= CFG.max_step + 1e-9 for w in res.weights.values())
