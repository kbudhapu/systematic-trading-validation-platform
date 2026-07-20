"""PAD audit P2 — math verification of cluster_brain arithmetic vs PAD v1.0 §3,
with worked examples (inputs -> expected by hand -> code output printed).

AUDIT ONLY: exercises the real allocator; no behavior change. Any divergence between
hand math and code output is a finding (see docs/PAD_AUDIT_2026-07.md).
"""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import (
    LegInput,
    PortfolioConfig,
    admission_decision,
    allocate,
    causal_vol,
    _apply_step_cap,
)

NOCAPS = dict(max_cluster=1.0, max_leg=1.0, shortvol_cluster_cap=1.0, vol_floor_factor=0.0)


def _series(scale: float, n: int = 120, seed: int = 0) -> np.ndarray:
    """Deterministic zero-mean weekly returns scaled to a controlled magnitude."""
    r = np.random.default_rng(seed).standard_normal(n)
    r = r - r.mean()
    return r * scale


def test_p2_1_level1_inverse_vol_and_vol_target_note():
    """§3 Level-1: budget share ∝ 1/cluster_vol, normalized. Also confirms the code
    does NOT scale to vol_target_annual (weights fully deploy to 1.0 pre-cap)."""
    cfg = PortfolioConfig(**NOCAPS)
    base = _series(1.0)
    legs = [
        LegInput("A", "C1", base * 0.01, oos_vol=0.01),
        LegInput("B", "C2", base * 0.015, oos_vol=0.015),
        LegInput("C", "C3", base * 0.03, oos_vol=0.03),
    ]
    res = allocate(legs, cfg)
    vols = res.vols
    leg2cluster = {"A": "C1", "B": "C2", "C": "C3"}
    inv = {k: 1.0 / v for k, v in vols.items()}
    tot = sum(inv.values())
    expected = {leg2cluster[k]: inv[k] / tot for k in inv}  # single-leg clusters
    print("\n[P2.1] cluster vols:", {k: round(v, 5) for k, v in vols.items()})
    print("[P2.1] expected (1/vol norm):", {k: round(v, 4) for k, v in expected.items()})
    print("[P2.1] code cluster_budgets :", {k: round(v, 4) for k, v in res.cluster_budgets.items()})
    for cl in expected:
        assert abs(res.cluster_budgets[cl] - expected[cl]) < 1e-9
    # vols in intended 1 : 1.5 : 3 ratio
    assert abs(vols["B"] / vols["A"] - 1.5) < 1e-6
    assert abs(vols["C"] / vols["A"] - 3.0) < 1e-6
    # NO vol_target scaling: full deployment (sum -> 1), cash ~ 0 with caps off
    print("[P2.1] cash_share (expect ~0, no vol_target scaling):", round(res.cash_share, 6))
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9
    assert res.cash_share < 1e-9


def test_p2_2_level2_split_and_cluster_budget_invariant_to_correlated_add():
    """§3 Level-2: within-cluster inverse-vol split; adding a REDUNDANT (correlated)
    leg to a cluster does NOT increase that cluster's budget (doctrine presumption)."""
    cfg = PortfolioConfig(**NOCAPS)
    a = _series(1.0, seed=1) * 0.01
    b = _series(1.0, seed=2) * 0.02
    # Case A: C1={L1}, C2={L2}
    resA = allocate([LegInput("L1", "C1", a, 0.01), LegInput("L2", "C2", b, 0.02)], cfg)
    # Case B: C1={L1, L1b(perfectly correlated, same vol)}, C2={L2}
    resB = allocate(
        [LegInput("L1", "C1", a, 0.01), LegInput("L1b", "C1", a.copy(), 0.01),
         LegInput("L2", "C2", b, 0.02)], cfg)
    budA = resA.cluster_budgets["C1"]
    budB = resB.cluster_budgets["C1"]
    print(f"\n[P2.2] cluster C1 budget: 1 leg={budA:.4f}  2 correlated legs={budB:.4f}")
    assert abs(budA - budB) < 1e-9, "correlated intra-cluster add must NOT change cluster budget"
    # within C1, two equal-vol legs split the cluster budget 50/50
    w = resB.weights
    print(f"[P2.2] within-C1 split: L1={w['L1']:.4f} L1b={w['L1b']:.4f} (expect equal)")
    assert abs(w["L1"] - w["L1b"]) < 1e-9
    assert abs((w["L1"] + w["L1b"]) - budB) < 1e-9


def test_p2_3_caps_bind_and_residual_to_cash():
    """§3 caps: single-leg 25%, SHORTVOL 15%, cluster 40%; residual -> CASH, reported."""
    cfg = PortfolioConfig()  # real caps
    lowvol = _series(1.0, seed=3) * 0.005   # very low vol -> wants a large share
    sv = _series(1.0, seed=4) * 0.006
    hi = _series(1.0, seed=5) * 0.05
    legs = [
        LegInput("BIG", "TREND", lowvol, 0.005),     # single leg -> 25% cap
        LegInput("SV", "SHORTVOL", sv, 0.006),        # SHORTVOL -> 15% cap
        LegInput("HI", "RV", hi, 0.05),
    ]
    res = allocate(legs, cfg)
    print("\n[P2.3] weights:", {k: round(v, 4) for k, v in res.weights.items()})
    print("[P2.3] cash_share:", round(res.cash_share, 4))
    assert res.weights["BIG"] <= 0.25 + 1e-9, "single-leg 25% cap"
    assert res.weights["SV"] <= 0.15 + 1e-9, "SHORTVOL 15% cap"
    deployed = sum(res.weights.values())
    assert abs(res.cash_share - (1.0 - deployed)) < 1e-9, "cash = 1 - deployed, reported"
    assert res.cash_share > 0.0, "caps must create reported cash drag here"


def test_p2_4_vol_floor_and_exposure_weighted():
    """§6.2 vol floor = 0.5 x registered OOS vol; exposure-weighted (flat weeks excluded)."""
    cfg = PortfolioConfig()
    # 2 active weeks (+/-0.02) at the END, within the last cluster_vol_window_weeks (26);
    # 58 leading flat weeks. causal_vol windows the trailing 26 weeks, so actives must be there.
    returns = np.array([0.0] * 58 + [0.02, -0.02])
    exposed = returns[np.abs(returns) > 0]
    realized = float(np.std(exposed, ddof=1))          # std of ONLY the 2 active weeks
    # floor inactive (small oos): causal_vol == realized (flat weeks excluded)
    cv_no_floor = causal_vol(returns, oos_vol=0.001, cfg=cfg)
    print(f"\n[P2.4] realized(exposed only)={realized:.5f}  causal_vol(no floor)={cv_no_floor:.5f}")
    assert abs(cv_no_floor - realized) < 1e-9, "exposure-weighted: flat weeks excluded from estimate"
    # floor active (large oos): causal_vol == 0.5 x oos_vol
    cv_floored = causal_vol(returns, oos_vol=0.10, cfg=cfg)
    print(f"[P2.4] 0.5*oos_vol={0.5*0.10:.5f}  causal_vol(floored)={cv_floored:.5f}")
    assert abs(cv_floored - 0.5 * 0.10) < 1e-9, "vol floor = 0.5 x registered OOS vol"
    # a naive full-window std (diluted by 58 flat weeks) would be far smaller -> proves exclusion matters
    naive = float(np.std(returns, ddof=1))
    print(f"[P2.4] naive full-window std={naive:.5f} (<< realized, shows dilution avoided)")
    assert naive < realized


def test_p2_5_rebalance_step_cap_over_two_rebalances():
    """§3 20% max step per rebalance: 0.12 -> 0.35 target moves 0.12->0.32->0.35."""
    cfg = PortfolioConfig()  # max_step = 0.20
    target = {"L": 0.35}
    r1 = _apply_step_cap(target, {"L": 0.12}, cfg)
    print(f"\n[P2.5] rebalance 1: prior 0.12 -> {r1['L']:.4f} (expect 0.32, +0.20 cap)")
    assert abs(r1["L"] - 0.32) < 1e-9
    r2 = _apply_step_cap(target, {"L": r1["L"]}, cfg)
    print(f"[P2.5] rebalance 2: prior {r1['L']:.2f} -> {r2['L']:.4f} (expect 0.35, only +0.03)")
    assert abs(r2["L"] - 0.35) < 1e-9


def test_p2_6_admission_blend_ci_non_degradation():
    """§2 admission: a candidate passing |rho|<0.35 but degrading the blend Sharpe-CI
    lower bound must DEFER_BLEND (not ADMIT)."""
    cfg = PortfolioConfig()  # min_overlap_weeks=52, max_abs_corr=0.35
    n = 60
    rng = np.random.default_rng(7)
    # active leg: positive drift, modest vol -> healthy Sharpe
    active_r = 0.01 + rng.standard_normal(n) * 0.01
    # candidate: decorrelated (independent draw) but zero/negative drift -> drags blend CI
    cand_r = -0.002 + rng.standard_normal(n) * 0.01
    active = [LegInput("ACT", "TREND", active_r, 0.01)]
    cand = LegInput("CAND", "ATTN", cand_r, 0.01)
    rho = float(np.corrcoef(cand_r, active_r)[0, 1])
    dec = admission_decision(cand, active, cfg)
    print(f"\n[P2.6] rho(cand,active)={rho:.3f} (<0.35 passes corr gate); verdict={dec.verdict}")
    assert abs(rho) < 0.35, "precondition: candidate passes the correlation gate"
    assert dec.verdict == "DEFER_BLEND", f"expected DEFER_BLEND, got {dec.verdict} {dec.detail}"
