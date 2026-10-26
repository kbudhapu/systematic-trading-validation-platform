"""Cost-stress battery (VTD doctrine section 1, Stage 4 / section 6).

Pure orchestration: re-runs an EXISTING backtest/sim under multiplied slippage
(1x / 2x / 4x, from `validation.cost_stress.slippage_multipliers`) and reports
the Sharpe at each multiplier plus a capacity-fragility flag. It does NOT
reimplement any sim -- the caller supplies a `run_fn(multiplier) -> sharpe` (or a
metrics dict containing "sharpe") that runs the real sim with slippage scaled by
`multiplier`. An edge that survives baseline costs but dies once slippage is
doubled is capacity-fragile: it depends on fills a real book cannot get.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

DEFAULT_MULTIPLIERS: tuple[float, ...] = (1.0, 2.0, 4.0)

RunFn = Callable[[float], "float | dict[str, Any]"]


def _as_sharpe(result: float | dict) -> float:
    if isinstance(result, dict):
        if "sharpe" not in result:
            raise KeyError("run_fn dict result must contain a 'sharpe' key")
        return float(result["sharpe"])
    return float(result)


def cost_stress_sweep(
    run_fn: RunFn,
    multipliers: Sequence[float] = DEFAULT_MULTIPLIERS,
    *,
    viability_sharpe: float = 0.0,
    fragility_multiplier: float = 2.0,
) -> dict:
    """Run `run_fn` at each slippage multiplier and summarise cost sensitivity.

    Returns:
      sharpe_by_multiplier: {multiplier: sharpe}
      monotone_non_increasing: Sharpe never rises as slippage rises (bool)
      dies_at: smallest multiplier at which Sharpe <= viability_sharpe (or None)
      capacity_fragile: viable at baseline (min multiplier) but not viable at the
        fragility_multiplier (default 2x) -- the doctrine's "dies-at-2x" flag
      viability_sharpe / fragility_multiplier: echoed for the report
    """
    mults = [float(m) for m in multipliers]
    if not mults:
        raise ValueError("at least one slippage multiplier is required")
    table = {m: _as_sharpe(run_fn(m)) for m in mults}

    ordered = sorted(table)
    monotone = all(
        table[ordered[i + 1]] <= table[ordered[i]] + 1e-12 for i in range(len(ordered) - 1)
    )
    dies_at = next((m for m in ordered if table[m] <= viability_sharpe), None)

    baseline = ordered[0]
    baseline_viable = table[baseline] > viability_sharpe
    frag_val = table.get(fragility_multiplier)
    capacity_fragile = bool(
        baseline_viable and frag_val is not None and frag_val <= viability_sharpe
    )
    return {
        "sharpe_by_multiplier": table,
        "monotone_non_increasing": monotone,
        "dies_at": dies_at,
        "capacity_fragile": capacity_fragile,
        "viability_sharpe": viability_sharpe,
        "fragility_multiplier": fragility_multiplier,
    }


def cost_observations(sweep: dict) -> dict:
    """Shape a sweep result into the cost_observations payload a DiagnosticReport
    carries (doctrine section 4), stringifying multiplier keys for JSON."""
    return {
        "sharpe_by_multiplier": {str(k): v for k, v in sweep["sharpe_by_multiplier"].items()},
        "capacity_fragile": sweep["capacity_fragile"],
        "dies_at": sweep["dies_at"],
        "monotone_non_increasing": sweep["monotone_non_increasing"],
    }
