"""PSD timeframe-menu resampler (doctrine section 1.1).

Resamples 15-minute SIP bars to the pre-committed coarse menu
{30m, 1h, 2h, 4h} plus a daily passthrough. Pure Polars functions, no I/O.

Design guarantees (all unit-tested):
- OHLCV aggregation: open=first, high=max, low=min, close=last, volume=sum.
- Session-boundary aware: a resampled bar NEVER aggregates across a session
  break (an overnight/holiday gap). Sessions are detected from timestamp GAPS
  only (a gap > 1.5x the source interval starts a new session), so a resampled
  bar's membership depends only on bar times, never on OHLCV values.
- Causal: each resampled bar depends only on the source bars in its own bucket
  (all at or before that bar's close), so a later source bar can never change an
  earlier resampled bar.
- Volume-conserving: within each session, sum(resampled volume) == sum(source
  volume), because every source bar lands in exactly one bucket.
"""

from __future__ import annotations

import polars as pl

_TARGET_MINUTES: dict[str, int | None] = {
    "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": None,  # None => whole-session
}


def _source_interval_minutes(ts: pl.Series) -> float:
    """Median positive gap between consecutive timestamps, in minutes."""
    if len(ts) < 2:
        return 15.0
    gaps = ts.sort().diff().drop_nulls().dt.total_seconds() / 60.0
    gaps = gaps.filter(gaps > 0)
    return float(gaps.median()) if len(gaps) else 15.0


def _with_sessions(bars: pl.DataFrame, source_minutes: float) -> pl.DataFrame:
    """Add a `_session` id: increments whenever the gap to the previous bar
    exceeds 1.5x the source interval (a session break)."""
    b = bars.sort("timestamp")
    gap_min = pl.col("timestamp").diff().dt.total_seconds() / 60.0
    is_break = (gap_min > 1.5 * source_minutes).fill_null(False)
    return b.with_columns(is_break.cum_sum().alias("_session"))


def resample(bars: pl.DataFrame, target: str) -> pl.DataFrame:
    """Resample `bars` (15m OHLCV+timestamp[+symbol]) to `target` timeframe.

    `target` in {"30m","1h","2h","4h","1d"}. Returns a new DataFrame with the
    same schema (timestamp = the bucket's first bar time).
    """
    if target not in _TARGET_MINUTES:
        raise ValueError(f"unsupported target {target!r}; menu = {list(_TARGET_MINUTES)}")
    if bars.is_empty():
        return bars.clear()

    src_min = _source_interval_minutes(bars["timestamp"])
    b = _with_sessions(bars, src_min)

    tgt_min = _TARGET_MINUTES[target]
    if tgt_min is None:
        # daily passthrough: one bar per session
        group_keys = ["_session"]
    else:
        factor = max(1, int(round(tgt_min / src_min)))
        # position within session, then integer-divide into buckets aligned to
        # each session's own start -> no bucket crosses a session break.
        b = b.with_columns(
            (pl.int_range(0, pl.len()).over("_session") // factor).alias("_bucket")
        )
        group_keys = ["_session", "_bucket"]

    has_symbol = "symbol" in b.columns
    aggs = [
        pl.col("timestamp").first().alias("timestamp"),
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    if has_symbol:
        aggs.append(pl.col("symbol").first().alias("symbol"))

    out = (
        b.group_by(group_keys, maintain_order=True)
        .agg(aggs)
        .drop(group_keys)
        .sort("timestamp")
    )
    cols = ["timestamp", "open", "high", "low", "close", "volume"] + (["symbol"] if has_symbol else [])
    return out.select(cols)


def resample_menu(bars: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Resample to every non-source menu timeframe (30m..4h) + daily."""
    return {tf: resample(bars, tf) for tf in ("30m", "1h", "2h", "4h", "1d")}
