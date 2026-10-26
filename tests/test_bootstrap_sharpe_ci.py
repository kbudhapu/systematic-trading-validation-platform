"""
Unit tests for the stationary block bootstrap Sharpe/Calmar CI
(Politis & Romano 1994 / Ledoit & Wolf 2008).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.chained_backtest import (
    bootstrap_sharpe_ci,
    stationary_bootstrap_resample,
)


def _make_ar1_returns(n: int = 200, phi: float = 0.7, sigma: float = 0.01, seed: int = 42) -> np.ndarray:
    """Synthetic AR(1) return series with strong positive autocorrelation."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    x[0] = rng.normal(0.001, sigma)
    for t in range(1, n):
        x[t] = 0.001 + phi * (x[t - 1] - 0.001) + rng.normal(0, sigma)
    return x


# ---------------------------------------------------------------------------
# Stationary bootstrap accounts for dependence (mean_block_length=1 is the
# i.i.d. special case: p=1/1=1.0 means every step restarts at a fresh random
# index, which is exactly i.i.d. resampling with replacement).
# ---------------------------------------------------------------------------

def test_stationary_ci_wider_than_iid_on_autocorrelated_series():
    returns = _make_ar1_returns()

    iid_result = bootstrap_sharpe_ci(
        returns, n_resamples=2000, mean_block_length=1, seed=1,
    )
    stationary_result = bootstrap_sharpe_ci(
        returns, n_resamples=2000, mean_block_length=10, seed=1,
    )

    iid_width = iid_result["sharpe"]["ci_upper"] - iid_result["sharpe"]["ci_lower"]
    stationary_width = stationary_result["sharpe"]["ci_upper"] - stationary_result["sharpe"]["ci_lower"]

    assert stationary_width > iid_width, (
        f"stationary bootstrap (block=10) CI width {stationary_width:.4f} should exceed "
        f"i.i.d. bootstrap (block=1) CI width {iid_width:.4f} on an autocorrelated series — "
        f"i.i.d. resampling destroys the dependence structure and understates uncertainty"
    )


def test_stationary_resample_with_block_length_one_matches_iid_statistics():
    """block=1 collapsing to i.i.d. resampling is the mechanism the width test
    above relies on — confirm it directly: resampled values are always drawn
    from the original set (a basic bootstrap invariant) and, for block=1,
    consecutive resampled indices should show ~zero autocorrelation even
    though the source series is strongly autocorrelated.
    """
    returns = _make_ar1_returns()
    rng = np.random.default_rng(7)
    resampled = stationary_bootstrap_resample(returns, mean_block_length=1, rng=rng)

    assert set(np.round(resampled, 10)).issubset(set(np.round(returns, 10)))
    resampled_autocorr = float(np.corrcoef(resampled[:-1], resampled[1:])[0, 1])
    assert abs(resampled_autocorr) < 0.3, (
        f"block=1 resample should show near-zero autocorrelation, got {resampled_autocorr:.3f}"
    )


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def test_reproducible_given_fixed_seed():
    returns = _make_ar1_returns()
    result_a = bootstrap_sharpe_ci(returns, n_resamples=500, seed=123)
    result_b = bootstrap_sharpe_ci(returns, n_resamples=500, seed=123)
    assert result_a == result_b


def test_different_seeds_give_different_results():
    returns = _make_ar1_returns()
    result_a = bootstrap_sharpe_ci(returns, n_resamples=500, seed=1)
    result_b = bootstrap_sharpe_ci(returns, n_resamples=500, seed=2)
    assert result_a["sharpe"]["ci_lower"] != result_b["sharpe"]["ci_lower"]


# ---------------------------------------------------------------------------
# Convergence: the CI estimate should stabilize (variance across independent
# runs shrinks) as n_resamples grows. A single pair of runs can be noisy, so
# this measures spread of the ci_lower estimate across several seeds at a
# small vs. large n_resamples, rather than asserting a single narrower/wider
# comparison.
# ---------------------------------------------------------------------------

def test_ci_estimate_stabilizes_as_n_resamples_increases():
    returns = _make_ar1_returns()
    seeds = range(10)

    small_lowers = [
        bootstrap_sharpe_ci(returns, n_resamples=30, seed=s)["sharpe"]["ci_lower"]
        for s in seeds
    ]
    large_lowers = [
        bootstrap_sharpe_ci(returns, n_resamples=3000, seed=s)["sharpe"]["ci_lower"]
        for s in seeds
    ]

    small_spread = float(np.std(small_lowers))
    large_spread = float(np.std(large_lowers))

    assert large_spread < small_spread, (
        f"ci_lower spread across seeds should shrink as n_resamples grows: "
        f"n=30 spread={small_spread:.4f}, n=3000 spread={large_spread:.4f}"
    )


# ---------------------------------------------------------------------------
# Ledoit & Wolf pass/fail signal
# ---------------------------------------------------------------------------

def test_excludes_zero_false_when_ci_contains_zero():
    """A returns series with mean near zero and high variance should fail
    the excludes_zero gate on both metrics."""
    rng = np.random.default_rng(99)
    noisy_returns = rng.normal(0.0, 0.05, size=100)
    result = bootstrap_sharpe_ci(noisy_returns, n_resamples=1000, seed=1)
    assert result["sharpe"]["excludes_zero"] is False


def test_excludes_zero_true_for_strong_positive_returns():
    rng = np.random.default_rng(11)
    strong_returns = rng.normal(0.02, 0.005, size=150)
    result = bootstrap_sharpe_ci(strong_returns, n_resamples=1000, seed=1)
    assert result["sharpe"]["excludes_zero"] is True


def test_raises_on_too_few_returns():
    import pytest
    with pytest.raises(ValueError):
        bootstrap_sharpe_ci([0.01], n_resamples=100)
