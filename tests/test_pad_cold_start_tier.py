"""FIX-C U3: cold-start tiered admission (dormant, default-OFF flag).

Below the tier floor -> DEFER; between tier floor and full overlap -> ADMIT at reduced budget
growing ~S*sqrt(t); full at the overlap floor. Default OFF preserves the strict 52wk floor.
"""
from __future__ import annotations

import numpy as np

from src.portfolio.cluster_brain import (
    LegInput, PortfolioConfig, _cold_start_budget_multiplier, admission_decision)

CFG_ON = PortfolioConfig(cold_start_tiered_admission=True)
CFG_OFF = PortfolioConfig()  # default -- strict 52wk floor


def _leg(name, cluster, seed, n, *, order=0):
    r = np.random.default_rng(seed).normal(0.003, 0.02, n)
    return LegInput(name, cluster, r, oos_vol=0.02, admission_order=order)


def test_multiplier_shape_sqrt_t():
    """The core U3 shape: 0 below the tier floor, the fraction AT the floor, 1.0 at full, monotone
    (sqrt(t)) between -- deterministic, no CI dependence."""
    assert _cold_start_budget_multiplier(25, CFG_ON) == 0.0            # below 26 -> deferred band
    assert _cold_start_budget_multiplier(26, CFG_ON) == 0.5            # tier floor -> fraction
    assert _cold_start_budget_multiplier(52, CFG_ON) == 1.0           # full overlap -> full budget
    assert _cold_start_budget_multiplier(100, CFG_ON) == 1.0          # capped at full
    m40 = _cold_start_budget_multiplier(40, CFG_ON)
    assert 0.5 < m40 < 1.0                                             # monotone in between
    assert _cold_start_budget_multiplier(30, CFG_ON) < m40            # grows with sqrt(t)


def test_tiered_defers_below_tier_floor():
    """A 20wk leg (< 26) still DEFERS even with tiering ON (option (a): zero budget below 26wk)."""
    active = [_leg("X", "ATTN", 5, 52)]
    d = admission_decision(_leg("Z", "RV", 7, 20), active, CFG_ON)
    assert d.verdict == "DEFER_OVERLAP"


def test_tiered_admits_reduced_budget_at_30wk():
    """A 30wk first leg (its own history is the evidence) admits at REDUCED (~half) budget, not full.
    First-leg path so the tiering is tested deterministically, independent of the CI/blend gates
    (those are exercised separately in the FIX-4 tests)."""
    d = admission_decision(_leg("Z", "RV", 7, 30), [], CFG_ON)
    assert d.verdict == "ADMIT"
    assert 0.5 <= d.budget_multiplier < 1.0
    assert d.detail["budget_multiplier"] == round(d.budget_multiplier, 4)


def test_tiered_full_budget_at_52wk():
    d = admission_decision(_leg("Z", "RV", 7, 52), [], CFG_ON)
    assert d.verdict == "ADMIT" and d.budget_multiplier == 1.0


def test_first_leg_below_tier_floor_defers_not_zero_budget():
    """A first leg with < 26wk own history DEFERS (never admits at zero budget)."""
    d = admission_decision(_leg("Z", "RV", 7, 18), [], CFG_ON)
    assert d.verdict == "DEFER_OVERLAP"


def test_default_off_keeps_strict_52wk_floor():
    """Flag OFF (default): a 40wk leg DEFERS (the tripwire behavior is preserved, budget 1.0)."""
    active = [_leg("X", "ATTN", 5, 52)]
    d = admission_decision(_leg("Z", "RV", 7, 40), active, CFG_OFF)
    assert d.verdict == "DEFER_OVERLAP"
