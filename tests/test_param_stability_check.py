"""
Unit tests for the parameter-neighborhood stability check.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.chained_backtest import (
    STABILITY_SPIKE_Z_THRESHOLD,
    _grid_step_neighbors,
    bar_sim_neighbor_params,
    compute_stability_score,
    mr_neighbor_params,
)


# ---------------------------------------------------------------------------
# compute_stability_score — the core statistic
# ---------------------------------------------------------------------------

def test_sharp_isolated_spike_scores_above_threshold():
    """One combo dramatically better than all neighbors -> high score, flagged."""
    winner_calmar = 10.0
    neighbor_calmars = [1.0, 1.2, 0.9, 1.1, 0.8, 1.0]
    score = compute_stability_score(winner_calmar, neighbor_calmars)
    assert score > STABILITY_SPIKE_Z_THRESHOLD


def test_broad_plateau_scores_below_threshold():
    """Several adjacent combos similarly good -> low score, not flagged."""
    winner_calmar = 5.1
    neighbor_calmars = [5.0, 5.2, 4.9, 5.15, 4.95, 5.05]
    score = compute_stability_score(winner_calmar, neighbor_calmars)
    assert score < STABILITY_SPIKE_Z_THRESHOLD


def test_stability_score_distinguishes_spike_from_plateau():
    spike_score = compute_stability_score(10.0, [1.0, 1.2, 0.9, 1.1])
    plateau_score = compute_stability_score(5.1, [5.0, 5.2, 4.9, 5.15])
    assert spike_score > plateau_score
    assert spike_score > STABILITY_SPIKE_Z_THRESHOLD
    assert plateau_score < STABILITY_SPIKE_Z_THRESHOLD


def test_empty_neighbors_returns_zero():
    assert compute_stability_score(10.0, []) == 0.0


def test_zero_variance_neighbors_matching_winner_is_stable():
    assert compute_stability_score(5.0, [5.0, 5.0, 5.0]) == 0.0


def test_zero_variance_neighbors_diverging_from_winner_is_infinite():
    score = compute_stability_score(10.0, [5.0, 5.0, 5.0])
    assert score == float("inf")


# ---------------------------------------------------------------------------
# Grid-step neighbor lookup
# ---------------------------------------------------------------------------

def test_grid_step_neighbors_interior_point():
    grid = [10, 20, 30, 40, 50]
    assert _grid_step_neighbors(grid, 30) == [20, 40]


def test_grid_step_neighbors_left_boundary():
    grid = [10, 20, 30, 40, 50]
    assert _grid_step_neighbors(grid, 10) == [20]


def test_grid_step_neighbors_right_boundary():
    grid = [10, 20, 30, 40, 50]
    assert _grid_step_neighbors(grid, 50) == [40]


def test_grid_step_neighbors_nearest_when_value_not_exact():
    grid = [10, 20, 30, 40, 50]
    # 31 is nearest to 30 -> same neighbors as an exact match on 30
    assert _grid_step_neighbors(grid, 31) == [20, 40]


# ---------------------------------------------------------------------------
# MR neighbor generation — one dimension perturbed at a time
# ---------------------------------------------------------------------------

def test_mr_neighbors_perturb_one_dimension_at_a_time():
    winner = {
        "sma_long": 48, "sma_short": 66,
        "long_sigma": 2.5, "short_sigma": 2.5,
        "exit_sigma": 0.10, "max_bars": 90,
    }
    neighbors = mr_neighbor_params(winner)
    assert len(neighbors) > 0
    for nb in neighbors:
        perturbed = nb["_perturbed"]
        for key in ("sma_long", "sma_short", "long_sigma", "short_sigma", "exit_sigma", "max_bars"):
            if key != perturbed:
                assert nb[key] == winner[key], f"{key} should be unchanged when perturbing {perturbed}"
        assert nb[perturbed] != winner[perturbed]


def test_mr_neighbors_respect_sma_ordering_constraint():
    """sma_long neighbors that would break sma_short > sma_long are skipped."""
    winner = {
        "sma_long": 65, "sma_short": 66,   # only 1 apart -> sma_long+1 step would violate
        "long_sigma": 2.5, "short_sigma": 2.5,
        "exit_sigma": 0.10, "max_bars": 90,
    }
    neighbors = mr_neighbor_params(winner)
    for nb in neighbors:
        assert nb["sma_short"] > nb["sma_long"]


# ---------------------------------------------------------------------------
# BTC / GLD-USO neighbor generation
# ---------------------------------------------------------------------------

def test_btc_neighbors_perturb_lookback_period():
    winner = {"lookback_period": 60}
    neighbors = bar_sim_neighbor_params(winner, "btc")
    assert len(neighbors) == 2  # interior point, both directions
    for nb in neighbors:
        assert nb["_perturbed"] == "lookback_period"
        assert nb["lookback_period"] != 60


def test_ema_neighbors_respect_fast_slow_ordering():
    winner = {"fast_ema_period": 15, "slow_ema_period": 60}
    neighbors = bar_sim_neighbor_params(winner, "ema")
    assert len(neighbors) > 0
    for nb in neighbors:
        assert nb["slow_ema_period"] > nb["fast_ema_period"]
