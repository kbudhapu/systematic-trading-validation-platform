"""Payoff-shape scalars from a per-trade / per-event net-return series.

These are the return SHAPE the experiment artifact schema records — `hit_rate`,
`mean_win`, `mean_loss`, `worst_trial_loss` — the fields a sizing or kill rule needs
that Sharpe / CI / MCPT cannot supply (mean, significance, dispersion-of-the-mean do
not distinguish a many-small-wins / rare-large-loss strategy from its mirror).

Producers compute these from the per-trade series AT THE POINT IT EXISTS and persist the
four scalars, then let the series (transient — retained only long enough to feed the
full-moment DSR) be popped as before. See docs/experiment_artifact_schema.md "Payoff shape".
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np


def payoff_shape(net_returns: Iterable[float]) -> dict[str, float]:
    """Return-shape scalars from a net-return series.

    - ``hit_rate``          fraction of trades with strictly positive net return
    - ``mean_win``          mean net return of winning trades (0.0 if none)
    - ``mean_loss``         mean net return of losing trades, signed <= 0 (0.0 if none)
    - ``worst_trial_loss``  the single most-negative net return (the left tail); equals
                            the smallest return if every trade won

    Returns an empty dict for an empty series (no trades -> no shape to persist), so a
    caller can ``stats.update(payoff_shape(series))`` without writing null keys.
    """
    net = np.asarray(list(net_returns), dtype=float)
    if net.size == 0:
        return {}
    wins = net[net > 0.0]
    losses = net[net < 0.0]
    return {
        "hit_rate": float(np.mean(net > 0.0)),
        "mean_win": float(wins.mean()) if wins.size else 0.0,
        "mean_loss": float(losses.mean()) if losses.size else 0.0,
        "worst_trial_loss": float(net.min()),
    }
