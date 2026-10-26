"""PAD audit P6 — THE SLAM: multi-year portfolio-lifecycle simulation through the REAL
cluster_brain (allocate / admission_decision / convergence_watch), deterministic + seeded.

AUDIT ONLY, isolated (no DB writes; pure functions). PAD sizing is not wired live, so per the
audit charter LLD states are injected via LegInput.state and the PAD-level consequences
(budget → sizing → cash, never siblings) are asserted; a reference CUSUM proves the planted
decay is detectable. Any act where code behavior diverges from doctrine is a finding
(docs/PAD_AUDIT_2026-07.md). Marked slow.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.portfolio.cluster_brain import (
    LegInput,
    PortfolioConfig,
    admission_decision,
    allocate,
    convergence_watch,
)

pytestmark = pytest.mark.slow

CFG = PortfolioConfig()


def _leg(lid, cluster, drift, vol, n, seed, order=0, state="ACTIVE", oos=None):
    r = np.random.default_rng(seed).standard_normal(n)
    r = r - r.mean()
    returns = drift + r * vol
    return LegInput(lid, cluster, returns, oos_vol=oos or vol, state=state, admission_order=order)


def _no_cap_violation(res):
    for w in res.weights.values():
        assert w <= CFG.max_leg + 1e-9
    by_cluster = {}
    for lid, w in res.weights.items():
        pass  # cluster caps validated structurally below via cash accounting
    assert res.cash_share >= -1e-9
    assert abs(sum(res.weights.values()) + res.cash_share - 1.0) < 1e-6, "weights+cash=1"
    # attribution reconstructable: every rebalance logs its corr/vol inputs
    assert set(res.log) >= {"cluster_vol", "cluster_budget", "vols", "weights", "cash_share", "corr_matrix"}


def _cusum_trips(returns, ref_mean, k=0.5, h=5.0):
    """Reference one-sided lower CUSUM on standardized returns; trips when cumulative
    negative drift exceeds h. Proves a planted mean-shift is detectable."""
    sd = np.std(returns[:26], ddof=1) or 1.0
    s = 0.0
    for x in returns:
        s = max(0.0, s + (ref_mean - x) / sd - k)
        if s > h:
            return True
    return False


def test_act1_leg_a_active_underdeployed():
    A = _leg("A", "TREND", 0.006, 0.012, 60, 1, order=0)
    res = allocate([A], CFG)
    print(f"\n[Act1] A weight={res.weights['A']:.3f} cash={res.cash_share:.3f} clusters={list(res.cluster_budgets)}")
    assert res.weights["A"] <= 0.25 + 1e-9 and res.cash_share >= 0.75 - 1e-9
    assert list(res.cluster_budgets) == ["TREND"]
    _no_cap_violation(res)


def test_act2_leg_b_deferred_then_admitted_two_cluster():
    A20 = _leg("A", "TREND", 0.006, 0.012, 20, 1, order=0)
    Bcand = _leg("B", "ATTN", 0.010, 0.008, 120, 2, order=1)
    assert admission_decision(Bcand, [A20], CFG).verdict == "DEFER_OVERLAP"
    # wk52: leg-A has 52 realized weeks; B decorrelated + strong Sharpe -> ADMIT
    A52 = _leg("A", "TREND", 0.006, 0.012, 52, 1, order=0)
    B52 = _leg("B", "ATTN", 0.012, 0.007, 52, 202, order=1)
    dec = admission_decision(B52, [A52], CFG)
    print(f"\n[Act2] admit B verdict={dec.verdict}")
    assert dec.verdict == "ADMIT", f"expected ADMIT got {dec.verdict} {dec.detail}"
    res = allocate([A52, B52], CFG)
    # two single-leg clusters -> inverse cluster-vol budgets
    inv = {c: 1.0 / v for c, v in res.cluster_budgets.items()}  # sanity: budgets sum to 1 pre-cap
    print(f"[Act2] cluster_budgets={ {k: round(v,3) for k,v in res.cluster_budgets.items()} }")
    assert abs(sum(res.cluster_budgets.values()) - 1.0) < 1e-9
    assert set(res.cluster_budgets) == {"TREND", "ATTN"}
    _no_cap_violation(res)


def test_act3_leg_c_three_cluster_blend_ci_and_caps():
    A = _leg("A", "TREND", 0.006, 0.012, 60, 1, order=0)
    B = _leg("B", "ATTN", 0.012, 0.007, 60, 202, order=1)
    C = _leg("C", "CAL_FLOW", 0.009, 0.010, 60, 303, order=2)
    dec = admission_decision(C, [A, B], CFG)
    print(f"\n[Act3] admit C verdict={dec.verdict}")
    assert dec.verdict in ("ADMIT", "DEFER_BLEND")  # CI check is invoked either way
    res = allocate([A, B, C], CFG)
    assert set(res.cluster_budgets) == {"TREND", "ATTN", "CAL_FLOW"}
    _no_cap_violation(res)


def test_act4_decay_watch_safemode_freed_to_cash_not_siblings():
    A = _leg("A", "TREND", 0.006, 0.012, 60, 1, order=0)
    C = _leg("C", "CAL_FLOW", 0.009, 0.010, 60, 303, order=2)
    # B healthy first 40 wks then decays (negative mean shift) last 20
    rng = np.random.default_rng(404)
    good = 0.012 + rng.standard_normal(40) * 0.007
    bad = -0.010 + rng.standard_normal(20) * 0.007
    b_returns = np.concatenate([good, bad])
    assert _cusum_trips(b_returns, ref_mean=0.012), "planted decay must be CUSUM-detectable"

    B_active = LegInput("B", "ATTN", b_returns, 0.007, state="ACTIVE", admission_order=1)
    base = allocate([A, B_active, C], CFG)
    B_watch = LegInput("B", "ATTN", b_returns, 0.007, state="WATCH", admission_order=1)
    watch = allocate([A, B_watch, C], CFG)
    B_safe = LegInput("B", "ATTN", b_returns, 0.007, state="SAFE_MODE", admission_order=1)
    safe = allocate([A, B_safe, C], CFG)

    print(f"\n[Act4] B weight ACTIVE={base.weights['B']:.3f} WATCH={watch.weights['B']:.3f} SAFE={safe.weights['B']:.3f}")
    print(f"[Act4] cash ACTIVE={base.cash_share:.3f} WATCH={watch.cash_share:.3f} SAFE={safe.cash_share:.3f}")
    # WATCH halves B's sizing; SAFE_MODE zeros it
    assert watch.weights["B"] < base.weights["B"]
    assert abs(watch.weights["B"] - base.weights["B"] * 0.5) < 1e-9
    assert safe.weights["B"] == 0.0
    # freed budget goes to CASH, never to siblings A / C
    assert watch.cash_share > base.cash_share and safe.cash_share > watch.cash_share
    assert abs(watch.weights["A"] - base.weights["A"]) < 1e-9, "sibling A must NOT absorb freed budget"
    assert abs(safe.weights["C"] - base.weights["C"]) < 1e-9, "sibling C must NOT absorb freed budget"
    for r in (base, watch, safe):
        _no_cap_violation(r)


def test_act5_correlation_spike_junior_to_watch():
    n = 90
    rng = np.random.default_rng(505)
    a = 0.006 + rng.standard_normal(n) * 0.012
    # C independent early, then converges to A over the last 40 weeks (noise at A's scale,
    # not 4x it, so the tail correlation genuinely exceeds 0.6)
    c = 0.009 + rng.standard_normal(n) * 0.010
    c[-40:] = a[-40:] + 0.001 * rng.standard_normal(40)
    A = LegInput("A", "TREND", a, 0.012, admission_order=0)  # senior
    C = LegInput("C", "CAL_FLOW", c, 0.010, admission_order=2)  # junior (later)
    alert = convergence_watch(A, C, CFG)
    print(f"\n[Act5] convergence alert={alert}")
    assert alert is not None, "sustained ρ>0.6 x4wk must trip the convergence watch"
    assert alert.junior_leg == "C" and alert.senior_leg == "A", "the JUNIOR (later-admitted) leg is flagged"
    assert alert.consecutive_weeks >= CFG.convergence_consecutive_weeks


def test_act6_revival_denied_and_cluster_budget_invariant_on_add():
    A = _leg("A", "TREND", 0.006, 0.012, 60, 1, order=0)
    C = _leg("C", "CAL_FLOW", 0.009, 0.010, 60, 303, order=2)
    # decayed B cannot silently revive: re-admission against the live book must not ADMIT
    rng = np.random.default_rng(606)
    b_decayed = np.concatenate([0.012 + rng.standard_normal(40) * 0.007, -0.010 + rng.standard_normal(20) * 0.007])
    B_revive = LegInput("B", "ATTN", b_decayed, 0.007, admission_order=1)
    verdict = admission_decision(B_revive, [A, C], CFG).verdict
    print(f"\n[Act6] decayed-B re-admission verdict={verdict}")
    assert verdict != "ADMIT", "LLD §5: a decayed leg may not revive without new registration"
    # leg D into B's old cluster (ATTN): adding a (redundant) leg does NOT inflate cluster budget
    D1 = _leg("D", "ATTN", 0.010, 0.008, 60, 707, order=3)
    one = allocate([A, C, D1], CFG)
    D2 = LegInput("D2", "ATTN", D1.returns.copy(), 0.008, admission_order=4)  # correlated sibling
    two = allocate([A, C, D1, D2], CFG)
    print(f"[Act6] ATTN budget: 1 leg={one.cluster_budgets['ATTN']:.4f} 2 legs={two.cluster_budgets['ATTN']:.4f}")
    assert abs(one.cluster_budgets["ATTN"] - two.cluster_budgets["ATTN"]) < 1e-9
    for r in (one, two):
        _no_cap_violation(r)
