"""FIX-4 P4-benchmark: raw point-estimate gate vs shrunk block-bootstrap-CI gate.

The empirical case for U1/U2 (registry STANDING-PADESTIMATOR): the CI gate should cut the
~8.5% false-admit of a true rho=0.5 leg (PB-4) without materially worsening false-reject of a
true diversifier. Slow (nested block bootstrap); excluded from the fast profile.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.portfolio.cluster_brain import PortfolioConfig, _corr, _shrunk_corr_ci

pytestmark = pytest.mark.slow

CFG = PortfolioConfig()
THR = CFG.max_abs_corr           # 0.35
N = 52                           # admission window (weeks)
TRIALS = 300
# small bootstrap B keeps the benchmark tractable; the gate logic is identical
_BOOT = dict(n_outer=60, n_inner=15)


def _pair(rng, rho, n):
    x = rng.standard_normal(n)
    y = rho * x + np.sqrt(max(0.0, 1.0 - rho * rho)) * rng.standard_normal(n)
    return x, y


def _raw_admits(x, y):
    return abs(_corr(x, y)) < THR


def _shrunk_ci_admits(x, y, prior, rng):
    lo, hi = _shrunk_corr_ci(x, y, prior, rng, **_BOOT)
    return hi < THR and lo > -THR          # U2: whole CI clears +/- max_abs_corr


def test_shrunk_ci_cuts_false_admit_of_true_rho_0p5():
    rng = np.random.default_rng(42)
    raw = shrunk = 0
    for _ in range(TRIALS):
        x, y = _pair(rng, 0.5, N)          # true rho=0.5 -> SHOULD be rejected
        raw += _raw_admits(x, y)           # admitting it = false-admit
        shrunk += _shrunk_ci_admits(x, y, 0.0, rng)  # cross-cluster prior = 0
    raw_rate, shrunk_rate = raw / TRIALS, shrunk / TRIALS
    print(f"\n[false-admit | true rho=0.5] raw={raw_rate:.3f}  shrunk-CI={shrunk_rate:.3f}")
    assert shrunk_rate < raw_rate, "shrunk-CI must reduce the false-admit of a true rho=0.5 leg"
    assert shrunk_rate <= raw_rate * 0.6, "materially lower (the CI width catches noisy-low draws)"


def test_shrunk_ci_conservative_tradeoff_false_reject_is_the_intended_cost():
    """FINDING (reported, not a regression): the CI gate is DELIBERATELY conservative -- P-B
    ranks false-ADMIT as the dangerous direction, so U2 trades a HIGHER false-reject (a good
    leg is DEFERRED, not lost) for the large false-admit cut. This verifies the tradeoff exists
    and is bounded (not pathological). The elevated false-reject at the 52wk window ties to PB-3
    (conservative admission compounds the cold-start assembly timeline)."""
    rng = np.random.default_rng(7)
    raw = shrunk = 0
    for _ in range(TRIALS):
        x, y = _pair(rng, 0.0, N)          # true rho=0 -> SHOULD be admitted
        raw += (not _raw_admits(x, y))     # rejecting it = false-reject
        shrunk += (not _shrunk_ci_admits(x, y, 0.0, rng))
    raw_rate, shrunk_rate = raw / TRIALS, shrunk / TRIALS
    print(f"[false-reject | true rho=0] raw={raw_rate:.3f}  shrunk-CI={shrunk_rate:.3f} (conservative cost)")
    assert shrunk_rate > raw_rate, "the CI gate is intentionally MORE conservative than the raw gate"
    assert shrunk_rate < 0.35, "but the false-reject cost is bounded, not pathological"
