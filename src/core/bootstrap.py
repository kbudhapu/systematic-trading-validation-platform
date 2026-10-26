"""Shared block-bootstrap resamplers (single source of truth).

`stationary_bootstrap_resample` (Politis & Romano, 1994) lives here so both the
chained backtester (`scripts/chained_backtest.py`) and the VTD SPA / stepwise
modules (`src/research/vtd/spa.py`) reuse ONE implementation -- imported, never
forked. Pure NumPy, no I/O.
"""

from __future__ import annotations

import numpy as np

# literature default (Politis & Romano); NOT tuned to this project's return series
BOOTSTRAP_DEFAULT_MEAN_BLOCK_LENGTH = 5


def stationary_bootstrap_resample(
    x: np.ndarray,
    mean_block_length: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """One stationary-bootstrap resample of x (Politis & Romano, 1994).

    Unlike a fixed-block bootstrap, block length here is itself random —
    drawn implicitly from a Geometric(p) distribution with p = 1/mean_block_length,
    by restarting at a fresh random index with probability p at each step and
    otherwise continuing circularly (index (i+1) mod n) from the previous
    draw. This is what makes the resampled series stationary regardless of
    the exact block-length choice, which a fixed-block bootstrap does not
    provide — the whole point of using this variant instead of a plain
    fixed-block or i.i.d. bootstrap.
    """
    n = len(x)
    if n == 0:
        return x.copy()
    p = 1.0 / max(mean_block_length, 1.0)
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(0, n)
    restarts = rng.random(n) < p
    for t in range(1, n):
        idx[t] = rng.integers(0, n) if restarts[t] else (idx[t - 1] + 1) % n
    return x[idx]
