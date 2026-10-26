"""Payoff-shape scalars — the return SHAPE producers persist from the per-trade series."""
from __future__ import annotations

from src.research.psd.payoff_shape import payoff_shape


def test_mixed_series():
    s = payoff_shape([0.02, -0.01, 0.03, -0.05, 0.0])
    assert s["hit_rate"] == 2 / 5  # 0.0 is NOT a win (strictly positive)
    assert abs(s["mean_win"] - 0.025) < 1e-12  # (0.02+0.03)/2
    assert abs(s["mean_loss"] - (-0.03)) < 1e-12  # (-0.01-0.05)/2
    assert s["worst_trial_loss"] == -0.05


def test_empty_series_is_no_shape():
    assert payoff_shape([]) == {}  # nothing to persist; caller writes no keys


def test_all_wins_has_zero_mean_loss():
    s = payoff_shape([0.01, 0.02])
    assert s["hit_rate"] == 1.0
    assert s["mean_loss"] == 0.0
    assert s["worst_trial_loss"] == 0.01  # left tail = smallest return when all won


def test_all_losses_has_zero_mean_win():
    s = payoff_shape([-0.01, -0.03])
    assert s["hit_rate"] == 0.0
    assert s["mean_win"] == 0.0
    assert s["worst_trial_loss"] == -0.03
