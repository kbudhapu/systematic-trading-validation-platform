"""
Unit tests for _deflated_sharpe_ratio and _adaptive_mr_training shape.

DSR formula: Bailey & López de Prado (2014).
Adaptive training: Optuna TPE replacing exhaustive MR grid search.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.chained_backtest import _deflated_sharpe_ratio


# ---------------------------------------------------------------------------
# DSR formula correctness
# ---------------------------------------------------------------------------

def test_dsr_returns_zero_for_n_trials_one():
    """With N=1 trial there is no multiple-testing correction; returns 0."""
    assert _deflated_sharpe_ratio(3.0, 1, 780) == 0.0


def test_dsr_returns_zero_for_n_train_one():
    assert _deflated_sharpe_ratio(3.0, 100, 1) == 0.0


def test_dsr_near_zero_for_zero_sharpe():
    """SR=0 cannot exceed the noise floor; DSR should be very small."""
    dsr = _deflated_sharpe_ratio(0.0, 1500, 780)
    assert dsr < 0.01, f"Expected DSR < 0.01 for SR=0, got {dsr:.4f}"


def test_dsr_matches_manual_calculation():
    """
    Manual calculation for SR_annual=3.0, N=1500, T=780 (SPY 30d, 15Min):

    ANNUALIZE = sqrt(252*26) ≈ 80.95
    SR_pbar   = 3.0 / 80.95 ≈ 0.03707
    sigma_SR  = sqrt((1 + 0.5 * 0.03707²) / 779) ≈ 0.03583
    q1 = Φ⁻¹(1-1/1500)           ≈ 3.208
    q2 = Φ⁻¹(1-e⁻¹/1500)         ≈ 3.476
    SR†= σ_SR × [(1-γ_EM)·q1 + γ_EM·q2] ≈ 0.1205
    z  = (0.03707 - 0.1205) / 0.03583 ≈ -2.33
    DSR= Φ(-2.33) ≈ 0.0099
    """
    dsr = _deflated_sharpe_ratio(3.0, 1500, 780)
    assert abs(dsr - 0.0099) < 0.001, f"DSR={dsr:.4f}, expected ≈0.0099"


def test_dsr_monotone_decreases_with_n_trials():
    """More trials → greater selection bias → lower DSR."""
    dsr_50   = _deflated_sharpe_ratio(3.0, 50,   780)
    dsr_500  = _deflated_sharpe_ratio(3.0, 500,  780)
    dsr_1500 = _deflated_sharpe_ratio(3.0, 1500, 780)
    assert dsr_50 > dsr_500 > dsr_1500, (
        f"DSR should decrease as N grows: {dsr_50:.4f} > {dsr_500:.4f} > {dsr_1500:.4f}"
    )


def test_dsr_monotone_increases_with_n_train():
    """More observations → tighter CI → DSR improves for the same SR."""
    dsr_780  = _deflated_sharpe_ratio(3.0, 1500, 780)
    dsr_2600 = _deflated_sharpe_ratio(3.0, 1500, 2600)
    dsr_5000 = _deflated_sharpe_ratio(3.0, 1500, 5000)
    assert dsr_780 < dsr_2600 < dsr_5000, (
        f"DSR should increase with T: {dsr_780:.4f} < {dsr_2600:.4f} < {dsr_5000:.4f}"
    )


def test_dsr_in_unit_interval():
    """DSR must always be in [0, 1]."""
    for sr in [0.0, 1.0, 3.0, 10.0, 50.0]:
        for n in [10, 100, 1500]:
            for t in [50, 780, 2600]:
                dsr = _deflated_sharpe_ratio(sr, n, t)
                assert 0.0 <= dsr <= 1.0, (
                    f"DSR out of [0,1]: sr={sr} n={n} t={t} → dsr={dsr}"
                )


def test_dsr_ceiling_note_gaussian_assumption():
    """
    Gaussian assumption (γ3=0, γ4=3) gives σ_SR = sqrt((1 + 0.5·SR²)/(T-1)).
    Fat-tailed returns have γ4 > 3, which would increase σ_SR and push DSR LOWER.
    So the reported DSR is a ceiling, not a floor.
    Check: the ceiling property holds — DSR(γ4=3) >= DSR(γ4=6) if we could compute both.
    We verify the formula consistency indirectly via the known boundary values.
    """
    # SR=3.0 annualized with 1500 trials on a 30-day window: expected DSR << 0.05
    dsr = _deflated_sharpe_ratio(3.0, 1500, 780)
    # Under Gaussian (ceiling), DSR ≈ 0.010; with real fat tails it would be lower
    assert dsr < 0.05, (
        f"Gaussian DSR={dsr:.4f} should be <0.05 (ceiling); "
        "fat tails would make it even lower"
    )
