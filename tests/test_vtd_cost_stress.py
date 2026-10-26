"""VTD Task 3 -- cost-stress battery. Sharpe must be monotone non-increasing in
the slippage multiplier, and a thin edge that dies at 2x must raise the
capacity-fragility flag; a robust edge must not."""
from __future__ import annotations

import pytest

from src.research.vtd.cost_stress import (
    DEFAULT_MULTIPLIERS, cost_observations, cost_stress_sweep,
)


def _linear_decay_sim(base_sharpe: float, per_mult_penalty: float):
    """A synthetic sim whose Sharpe falls linearly with the slippage multiplier
    (higher costs never help) -- ground truth for monotonicity."""
    def run(mult: float) -> float:
        return base_sharpe - per_mult_penalty * (mult - 1.0)
    return run


def test_default_multipliers_match_doctrine() -> None:
    assert DEFAULT_MULTIPLIERS == (1.0, 2.0, 4.0)


def test_sharpe_monotone_non_increasing() -> None:
    sweep = cost_stress_sweep(_linear_decay_sim(1.2, 0.15))
    vals = [sweep["sharpe_by_multiplier"][m] for m in (1.0, 2.0, 4.0)]
    print(f"\nsharpe by multiplier: {vals}")
    assert sweep["monotone_non_increasing"] is True
    assert vals[0] >= vals[1] >= vals[2]


def test_robust_edge_not_capacity_fragile() -> None:
    """A strong edge (Sharpe stays well above zero through 4x) is not fragile."""
    sweep = cost_stress_sweep(_linear_decay_sim(1.5, 0.1))
    assert sweep["capacity_fragile"] is False
    assert sweep["dies_at"] is None


def test_thin_edge_dies_at_2x_flags_fragility() -> None:
    """A thin edge alive at 1x but negative by 2x -> capacity_fragile True and
    dies_at == 2.0 (the doctrine 'dies-at-2x' capacity flag)."""
    # 0.30 at 1x, -0.10 at 2x, -0.90 at 4x
    sweep = cost_stress_sweep(_linear_decay_sim(0.30, 0.40))
    print(f"\nthin-edge sweep: {sweep['sharpe_by_multiplier']}  fragile={sweep['capacity_fragile']}")
    assert sweep["capacity_fragile"] is True
    assert sweep["dies_at"] == 2.0
    assert sweep["monotone_non_increasing"] is True


def test_edge_dying_at_4x_not_flagged_as_2x_fragile() -> None:
    """An edge that only dies at 4x is NOT flagged capacity-fragile (the flag is
    specifically 'dies at 2x'), though dies_at records 4.0."""
    # 0.5 at 1x, 0.2 at 2x, -0.4 at 4x
    sweep = cost_stress_sweep(_linear_decay_sim(0.5, 0.30))
    assert sweep["capacity_fragile"] is False
    assert sweep["dies_at"] == 4.0


def test_accepts_metrics_dict_result() -> None:
    def run(mult: float) -> dict:
        return {"sharpe": 1.0 - 0.2 * (mult - 1.0), "trades": 100}
    sweep = cost_stress_sweep(run)
    assert sweep["sharpe_by_multiplier"][1.0] == 1.0
    assert sweep["monotone_non_increasing"] is True


def test_dict_result_missing_sharpe_raises() -> None:
    with pytest.raises(KeyError):
        cost_stress_sweep(lambda m: {"return": 0.1})


def test_cost_observations_shape_for_report() -> None:
    """cost_observations() produces a JSON-friendly payload for the
    DiagnosticReport (string multiplier keys)."""
    sweep = cost_stress_sweep(_linear_decay_sim(0.30, 0.40))
    obs = cost_observations(sweep)
    assert set(obs["sharpe_by_multiplier"]) == {"1.0", "2.0", "4.0"}
    assert obs["capacity_fragile"] is True
    assert obs["dies_at"] == 2.0


def test_non_monotone_sim_detected() -> None:
    """If a (pathological) sim's Sharpe rises with cost, monotone flag is False --
    a data-quality signal, not silently ignored."""
    table = {1.0: 0.5, 2.0: 0.7, 4.0: 0.3}
    sweep = cost_stress_sweep(lambda m: table[m])
    assert sweep["monotone_non_increasing"] is False
