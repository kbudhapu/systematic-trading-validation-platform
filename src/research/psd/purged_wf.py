"""Purged + embargoed walk-forward masking (doctrine S6).

Wraps an existing walk-forward split (train/test index arrays) and removes
label leakage at every train/test boundary:

- PURGE: drop training bars whose forward label/holding window [t, t+E] overlaps
  the test block. E = max(max_bars_in_trade, signal_lookback) from the leg's own
  config (asserted, never guessed) -- doctrine adversarial-audit item 3.
- EMBARGO: drop training bars in [test_end, test_end + E) after each test block,
  before training data resumes.

Pure functions over bar indices, drop-in compatible with an existing runner's
split interface (list of (train_idx, test_idx) -> same, masked). Future legs
adopt this by importing `purge_embargo_splits`, not by rewriting their runner.

E derivation: for an intraday leg, E resolves to `max_bars_in_trade` bars (a few trading days); for a longer-horizon leg, E resolves to the label horizon expressed in the leg's own bars.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

# config keys that express a signal lookback horizon (max of those present)
_LOOKBACK_KEYS = (
    "signal_lookback", "sma_period_long", "sma_period_short", "sma_period",
    "lookback_period", "vwap_period", "slow_ema_period", "fast_ema_period",
)


def derive_embargo(config: dict) -> int:
    """E = max(max_bars_in_trade, signal_lookback), from the leg's own config.

    signal_lookback = the largest lookback-horizon parameter present. Raises if
    neither a holding period nor any lookback can be found (never silently 0).
    """
    max_bars = int(config.get("max_bars_in_trade", 0) or 0)
    lookback = 0
    for k in _LOOKBACK_KEYS:
        if k in config and config[k] is not None:
            lookback = max(lookback, int(config[k]))
    e = max(max_bars, lookback)
    if e <= 0:
        raise ValueError(
            "derive_embargo: config has neither max_bars_in_trade nor a lookback "
            f"horizon; cannot size E from {sorted(config)!r}"
        )
    return e


def purge_embargo_train(train_idx: np.ndarray, test_start: int, test_end: int,
                        e: int) -> np.ndarray:
    """Return `train_idx` with purge + embargo applied for one contiguous test
    block [test_start, test_end).

    PURGE: drop t whose label window [t, t+E] overlaps [test_start, test_end)
           i.e. (t + E >= test_start) and (t < test_end).
    EMBARGO: drop t in [test_end, test_end + E).
    """
    t = np.asarray(train_idx, dtype=np.int64)
    label_end = t + e
    purged = (label_end >= test_start) & (t < test_end)
    embargoed = (t >= test_end) & (t < test_end + e)
    return t[~(purged | embargoed)]


def purge_embargo_splits(
    splits: Sequence[tuple[np.ndarray, np.ndarray]], e: int
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Apply purge+embargo to each (train_idx, test_idx) split. `test_idx` must be
    a contiguous block; returns (masked_train_idx, test_idx) per split -- same
    interface, so it is drop-in for an existing walk-forward runner."""
    out = []
    for train_idx, test_idx in splits:
        test = np.asarray(test_idx, dtype=np.int64)
        if test.size == 0:
            out.append((np.asarray(train_idx, dtype=np.int64), test)); continue
        ts, te = int(test.min()), int(test.max()) + 1
        if not np.array_equal(test, np.arange(ts, te)):
            raise ValueError("purge_embargo_splits: test_idx must be a contiguous block")
        out.append((purge_embargo_train(train_idx, ts, te, e), test))
    return out


def quarterly_splits(n_bars: int, test_block: int, n_splits: int | None = None,
                     min_train: int = 1) -> list[tuple[np.ndarray, np.ndarray]]:
    """Generate anchored walk-forward splits: consecutive contiguous test blocks of
    `test_block` bars, training on all bars before each test block. This is the
    raw (unpurged) split spec that `purge_embargo_splits` then masks -- provided so
    a leg can build splits without a custom generator."""
    splits = []
    start = min_train
    while start + test_block <= n_bars:
        test = np.arange(start, start + test_block)
        train = np.arange(0, start)
        splits.append((train, test))
        start += test_block
        if n_splits is not None and len(splits) >= n_splits:
            break
    return splits
