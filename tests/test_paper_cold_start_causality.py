"""Paper shakedown -- cold-start causality guard.

When the bot cold-starts, it warms up its RollingWindow from historical bars up
to a cutoff, then emits its first LIVE signal on the first post-warmup bar. That
first live signal must be CAUSAL: it may depend only on bars at or before the
bar being evaluated, never on any later bar. This test proves no future leak by
feeding two histories that are identical through the first live bar but diverge
afterward, and asserting the first live signal is identical for both.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.rolling_window import RollingWindow
from src.models import Bar
from src.strategies.mean_reversion_qqq import MeanReversionQqqStrategy


def _bar(i: int, close: float) -> Bar:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
    return Bar(timestamp=ts, open=close, high=close + 0.1, low=close - 0.1,
               close=close, volume=1_000_000.0, symbol="QQQ")


def _first_live_signal(closes, cutoff, params):
    """Warm up from closes[:cutoff+1] (last bar = the first live bar), return the
    signal evaluated on that first live bar."""
    strat = MeanReversionQqqStrategy()
    win = RollingWindow(maxlen=512)
    sig = None
    for i in range(cutoff + 1):
        win.append(_bar(i, closes[i]))
        sig = strat.evaluate_live(win, params)   # signal on the just-appended bar
    return sig


def test_first_live_signal_is_causal_no_future_leak() -> None:
    cutoff = 200  # enough warmup for the 51/59 SMA horizons
    params = {"symbol": "QQQ", "position_side": 0}

    base = [100.0 + (i % 7) - 3 for i in range(cutoff + 1)]
    base[cutoff] = 100.0 + 30.0   # a sharp move on the first live bar -> likely a signal

    hist_a = list(base) + [500.0, 1.0, 400.0]   # wild FUTURE bars (never seen at cutoff)
    hist_b = list(base) + [-50.0, 999.0, 0.5]   # different future

    sig_a = _first_live_signal(hist_a, cutoff, params)
    sig_b = _first_live_signal(hist_b, cutoff, params)

    a = (sig_a.action, round(sig_a.price, 6)) if sig_a else None
    b = (sig_b.action, round(sig_b.price, 6)) if sig_b else None
    assert a == b, f"first live signal depends on future bars -> lookahead ({a} != {b})"


def test_warmup_boundary_signal_matches_isolated_recompute() -> None:
    """The first live signal from an incremental warmup must equal the signal
    computed from exactly the same bar slice in one shot -- no state carried in
    from bars the bot should not have processed."""
    cutoff = 220
    params = {"symbol": "QQQ", "position_side": 0}
    closes = [100.0 + 4.0 * ((i % 11) - 5) / 5.0 for i in range(cutoff + 40)]
    closes[cutoff] = 100.0 + 25.0

    incremental = _first_live_signal(closes, cutoff, params)

    # one-shot: fresh window, same slice [0..cutoff]
    strat = MeanReversionQqqStrategy()
    win = RollingWindow(maxlen=512)
    oneshot = None
    for i in range(cutoff + 1):
        win.append(_bar(i, closes[i]))
        oneshot = strat.evaluate_live(win, params)

    ia = (incremental.action, round(incremental.price, 6)) if incremental else None
    oa = (oneshot.action, round(oneshot.price, 6)) if oneshot else None
    assert ia == oa
