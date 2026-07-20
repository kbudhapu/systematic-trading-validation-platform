"""Masters-style Monte-Carlo Permutation Test (VTD doctrine Stage 1, section 2).

Early-stage, cheap **kill-gate**: it asks whether a strategy's in-sample
performance could plausibly be produced by chance on data with the same
per-session return distribution but no exploitable temporal structure. Per the
doctrine (V1 / section 5.1) an MCPT *pass* is weak evidence -- IS plus selection
-- and its ONLY licensed use is clearing the kill-gate; it is logged as
exploration and is NEVER citable as leg evidence.

Permutation follows Masters: within each trading session the per-bar LOG changes
(open/high/low/close increments off the prior close, carrying volume) are
block-permuted, then re-integrated by exponentiation off the session's first
bar. Because a session's set of per-bar increments is only reordered:
  * the per-session distribution of returns, ranges and volume is preserved
    exactly (moments, totals), while
  * temporal order -- hence any autocorrelation the strategy could exploit --
    is destroyed.
Session boundaries are never crossed (block permutation is within-session), so
overnight/holiday gaps stay intact. Session detection reuses the PSD resampler's
logic (imported, not forked).

p = (1 + #{perm_stat >= real_stat}) / (1 + n_perm),  n_perm >= 1000 in production.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable

import numpy as np
import polars as pl

from src.research.psd.resample import _source_interval_minutes, _with_sessions

StrategyFn = Callable[[pl.DataFrame], float]

_OHLC = ("open", "high", "low", "close")


def _resolve_block(block: int | str, lookback: int | None) -> int:
    """block="auto" -> the strategy's max lookback (its dependence horizon)."""
    if isinstance(block, str):
        if block != "auto":
            raise ValueError(f"block must be a positive int or 'auto', got {block!r}")
        if lookback is None or lookback < 1:
            raise ValueError("block='auto' requires a positive `lookback` (strategy max lookback)")
        return int(lookback)
    if block < 1:
        raise ValueError("block length must be >= 1")
    return int(block)


def _block_order(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """A block permutation of range(n): partition into contiguous blocks of
    `block`, shuffle the block ORDER, concatenate. This is a permutation of the
    index multiset, so any per-element quantity is preserved as a multiset."""
    if n <= 1:
        return np.arange(n, dtype=np.int64)
    starts = np.arange(0, n, block)
    blocks = [np.arange(s, min(s + block, n), dtype=np.int64) for s in starts]
    perm = rng.permutation(len(blocks))
    return np.concatenate([blocks[i] for i in perm]) if blocks else np.arange(n, dtype=np.int64)


def permute_bars(
    bars: pl.DataFrame,
    block: int | str = "auto",
    *,
    lookback: int | None = None,
    session_aware: bool = True,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """Return one session-aware block permutation of `bars` (Masters method).

    Timestamps and per-session bar count are held fixed; only the OHLCV *content*
    (via per-bar log increments off the prior close) is reordered within each
    session, so session structure and boundaries are preserved by construction.
    """
    if bars.is_empty():
        return bars.clear()
    b = bars.sort("timestamp")
    if session_aware:
        src_min = _source_interval_minutes(b["timestamp"])
        b = _with_sessions(b, src_min)
    else:
        b = b.with_columns(pl.lit(0).alias("_session"))

    blk = _resolve_block(block, lookback)
    ts = b["timestamp"].to_list()
    has_symbol = "symbol" in b.columns
    symbols = b["symbol"].to_list() if has_symbol else None
    sessions = b["_session"].to_list()
    logc = {c: np.log(b[c].to_numpy().astype(np.float64)) for c in _OHLC}
    volume = b["volume"].to_numpy().astype(np.float64)

    n = b.height
    out_o = np.empty(n); out_h = np.empty(n); out_l = np.empty(n)
    out_c = np.empty(n); out_v = np.empty(n)

    # per-session reconstruction. Masters convention: the session's FIRST bar is
    # the fixed anchor (unchanged); only the per-bar log increments of the
    # remaining bars are block-permuted, then re-integrated off the anchor.
    start = 0
    while start < n:
        end = start
        while end < n and sessions[end] == sessions[start]:
            end += 1
        m = end - start
        # anchor bar unchanged
        for c, dst in zip(_OHLC, (out_o, out_h, out_l, out_c)):
            dst[start] = np.exp(logc[c][start])
        out_v[start] = volume[start]
        if m == 1:
            start = end
            continue
        anchor_close = logc["close"][start]
        # increments of bars 1..m-1 off the PRIOR close
        prior = logc["close"][start:end - 1]                 # closes of bars 0..m-2
        incr = {c: logc[c][start + 1:end] - prior for c in _OHLC}
        vol_tail = volume[start + 1:end]

        order = _block_order(m - 1, blk, rng)                # permute the tail only
        perm_dc = incr["close"][order]
        # running prior close for each tail bar: anchor, then compounding
        run_prior = anchor_close + np.concatenate(([0.0], np.cumsum(perm_dc)[:-1]))
        tail = slice(start + 1, end)
        out_c[tail] = np.exp(run_prior + perm_dc)
        out_o[tail] = np.exp(run_prior + incr["open"][order])
        out_h[tail] = np.exp(run_prior + incr["high"][order])
        out_l[tail] = np.exp(run_prior + incr["low"][order])
        out_v[tail] = vol_tail[order]
        start = end

    cols = {
        "timestamp": ts, "open": out_o, "high": out_h, "low": out_l,
        "close": out_c, "volume": out_v,
    }
    if has_symbol:
        cols["symbol"] = symbols
    order_cols = ["timestamp", "open", "high", "low", "close", "volume"] + (["symbol"] if has_symbol else [])
    return pl.DataFrame(cols).select(order_cols)


# ---- parallel permutation evaluation (spawn-context week-level pool) ----------

_POOL_CTX = {}


def _eval_one(args) -> float:
    strategy_fn, bars, block, lookback, session_aware, seed = args
    rng = np.random.default_rng(seed)
    perm = permute_bars(bars, block, lookback=lookback, session_aware=session_aware, rng=rng)
    return float(strategy_fn(perm))


def mcpt_pvalue(
    strategy_fn: StrategyFn,
    bars: pl.DataFrame,
    *,
    n_perm: int = 1000,
    seed: int = 0,
    block: int | str = "auto",
    lookback: int | None = None,
    session_aware: bool = True,
    n_jobs: int = 1,
) -> dict:
    """MCPT p-value for `strategy_fn` on `bars`.

    `strategy_fn(bars) -> float` returns a performance statistic where LARGER is
    better (Sharpe, total log return, ...). Each permutation is seeded
    deterministically (seed + 1 + i) so the result is reproducible. With
    ``n_jobs != 1`` permutations run on a spawn-context process pool -- the same
    embarrassingly-parallel week-level pattern the backtester uses; the default
    (serial) path is deterministic and dependency-free for tests.

    Returns {"p_value", "real_stat", "n_perm", "n_ge", "block"}.
    """
    if n_perm < 1:
        raise ValueError("n_perm must be >= 1")
    real_stat = float(strategy_fn(bars))
    blk = _resolve_block(block, lookback)
    tasks = [
        (strategy_fn, bars, block, lookback, session_aware, seed + 1 + i)
        for i in range(n_perm)
    ]

    if n_jobs == 1:
        perm_stats = [_eval_one(t) for t in tasks]
    else:
        ctx = multiprocessing.get_context("spawn")
        workers = n_jobs if n_jobs > 0 else (multiprocessing.cpu_count() or 1)
        with ctx.Pool(processes=workers) as pool:
            perm_stats = pool.map(_eval_one, tasks)

    n_ge = int(sum(1 for s in perm_stats if s >= real_stat))
    p_value = (1 + n_ge) / (1 + n_perm)
    return {
        "p_value": p_value, "real_stat": real_stat, "n_perm": n_perm,
        "n_ge": n_ge, "block": blk,
    }
