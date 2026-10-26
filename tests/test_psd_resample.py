"""PSD resampler tests: causality, session boundaries, volume conservation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from src.research.psd.resample import resample


def _bars(closes, *, start=None, step_min=15, symbol="QQQ", gaps=None):
    """Build 15m bars. `gaps` = dict{index: extra_minutes} to inject a session
    break before that bar."""
    start = start or datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)  # a Monday 09:30 ET
    rows = []
    t = start
    for i, c in enumerate(closes):
        if gaps and i in gaps:
            t = t + timedelta(minutes=gaps[i])
        rows.append({"timestamp": t, "open": c, "high": c + 1, "low": c - 1,
                     "close": c, "volume": 100.0 + i, "symbol": symbol})
        t = t + timedelta(minutes=step_min)
    return pl.DataFrame(rows)


def test_ohlcv_aggregation_correct() -> None:
    bars = _bars([10, 11, 12, 13], step_min=15)   # 4 x 15m -> one 1h bar
    out = resample(bars, "1h")
    assert out.height == 1
    r = out.row(0, named=True)
    assert r["open"] == 10 and r["close"] == 13
    assert r["high"] == 13 + 1 and r["low"] == 10 - 1
    assert r["volume"] == sum(100.0 + i for i in range(4))


def test_causality_future_bar_does_not_change_prior_resampled() -> None:
    closes = list(range(40))
    base = _bars(closes)
    out_base = resample(base, "1h")
    # mutate a LATE bar's OHLCV (bar 38); its bucket is the last one
    mut_closes = list(closes); mut_closes[38] = 9999
    out_mut = resample(_bars(mut_closes), "1h")
    # every resampled bar strictly before the mutated bar's bucket must match
    mut_bucket_start = base["timestamp"][ (38 // 4) * 4 ]
    a = out_base.filter(pl.col("timestamp") < mut_bucket_start)
    b = out_mut.filter(pl.col("timestamp") < mut_bucket_start)
    assert a.equals(b) and a.height > 0, "a future 15m bar changed an earlier resampled bar -> non-causal"


def test_session_boundary_no_spanning_bar() -> None:
    # 6 bars, then an overnight gap (~17.5h), then 6 more bars. A 2h (8x15m)
    # bucket must NOT merge across the gap.
    closes = list(range(12))
    bars = _bars(closes, gaps={6: 17 * 60 + 30})
    out = resample(bars, "2h")
    # each resampled bar's window must lie entirely within one session:
    # reconstruct sessions in source by the gap, assert no resampled bar's
    # [open_ts, close_ts] straddles the gap boundary.
    gap_before = bars["timestamp"][6]
    # a resampled bar "spans" if it opens before the gap but its bucket also
    # pulled bars after the gap. With 8x buckets and only 6 pre-gap bars, the
    # pre-gap session yields its own (partial) bar ending before the gap.
    pre = out.filter(pl.col("timestamp") < gap_before)
    post = out.filter(pl.col("timestamp") >= gap_before)
    assert pre.height >= 1 and post.height >= 1
    # the last pre-gap resampled bar must be built only from pre-gap source bars:
    # its close equals the last pre-gap source close (index 5), not a post-gap value
    assert pre.row(pre.height - 1, named=True)["close"] == closes[5]
    assert post.row(0, named=True)["open"] == closes[6]


def test_volume_conserved_per_session() -> None:
    closes = list(range(20))
    bars = _bars(closes, gaps={10: 17 * 60 + 30})  # two sessions of 10 bars
    for tf in ("30m", "1h", "2h", "4h", "1d"):
        out = resample(bars, tf)
        assert abs(out["volume"].sum() - bars["volume"].sum()) < 1e-9, f"{tf}: volume not conserved"


def test_daily_passthrough_one_bar_per_session() -> None:
    bars = _bars(list(range(20)), gaps={10: 17 * 60 + 30})   # 2 sessions
    out = resample(bars, "1d")
    assert out.height == 2
    assert out.row(0, named=True)["open"] == 0 and out.row(0, named=True)["close"] == 9
    assert out.row(1, named=True)["open"] == 10 and out.row(1, named=True)["close"] == 19
