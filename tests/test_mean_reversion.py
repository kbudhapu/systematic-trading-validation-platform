"""Tests for mean reversion strategy and Numba indicators."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from src.core.rolling_window import RollingWindow
from src.math.indicators import (
    LONG,
    EXIT,
    compute_atr,
    mean_reversion_action,
    mean_reversion_action_asymmetric,
    rolling_sma,
    rolling_std,
    scan_mean_reversion_signals,
    scan_mean_reversion_signals_asymmetric,
)
from src.models import SignalAction
from src.strategies.mean_reversion_spy import MeanReversionSpyStrategy


def _bar(close: float, i: int = 0) -> Bar:
    from src.models import Bar

    return Bar(
        timestamp=datetime(2024, 1, 1, 10, i, tzinfo=timezone.utc),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1_000_000.0,
        symbol="SPY",
    )


def test_rolling_window_deque_append():
    window = RollingWindow(maxlen=5)
    for i in range(5):
        assert window.append(_bar(100.0, i))
    assert len(window) == 5
    assert not window.append(_bar(100.0, 4))


def test_rolling_window_ring_buffer_order():
    window = RollingWindow(maxlen=3)
    for i, price in enumerate([10.0, 20.0, 30.0, 40.0]):
        window.append(_bar(price, i))
    closes = window.closes_array()
    assert list(closes) == [20.0, 30.0, 40.0]


def test_tail_closes():
    window = RollingWindow(maxlen=10)
    for i in range(5):
        window.append(_bar(float(i), i))
    tail = window.tail_closes(3)
    assert list(tail) == [2.0, 3.0, 4.0]


def test_rolling_window_corporate_action_offset():
    window = RollingWindow(maxlen=5)
    for i, price in enumerate([100.0, 101.0, 102.0]):
        window.append(_bar(price, i))
    assert window.apply_corporate_action_offset(1.5) is True
    assert list(window.closes_array()) == [101.5, 102.5, 103.5]
    assert window.corporate_action_offset_applied == 1.5
    assert window.apply_corporate_action_offset(1.5) is False


def test_numba_sma_std():
    arr = np.array([100.0] * 19 + [80.0], dtype=np.float64)
    sma = rolling_sma(arr, 20)
    std = rolling_std(arr, 20)
    assert sma == 99.0
    assert std > 0


def test_scan_mean_reversion_o_n():
    closes = np.array([100.0] * 25 + [80.0] * 5, dtype=np.float64)
    actions = scan_mean_reversion_signals(closes, 20, 1.5, 0.1)
    assert actions[-1] == LONG


def test_mean_reversion_live_long():
    window = RollingWindow(maxlen=30)
    for i in range(19):
        window.append(_bar(100.0, i))
    for i, price in enumerate([90.0, 88.0, 85.0, 82.0, 80.0], start=19):
        window.append(_bar(price, i))

    strategy = MeanReversionSpyStrategy()
    signal = strategy.evaluate_live(
        window,
        {
            "sma_period": 20,
            "long_threshold_sigma": 1.5,
            "short_threshold_sigma": 2.0,
            "exit_sigma": 0.1,
        },
    )
    assert signal is not None
    assert signal.action == SignalAction.LONG


def test_mean_reversion_time_stop():
    window = RollingWindow(maxlen=60)
    for i in range(55):
        window.append(_bar(100.0, i))

    strategy = MeanReversionSpyStrategy()
    signal = strategy.evaluate_live(
        window,
        {
            "sma_period": 20,
            "long_threshold_sigma": 2.75,
            "short_threshold_sigma": 2.25,
            "exit_sigma": 0.4,
            "max_bars_in_trade": 40,
            "bars_in_trade": 40,
            "in_position": True,
        },
    )
    assert signal is not None
    assert signal.action == SignalAction.EXIT
    assert signal.metadata.get("reason") == "time_stop"


def test_asymmetric_scan_long_vs_short():
    closes = np.array([100.0] * 25 + [115.0] * 5, dtype=np.float64)
    actions = scan_mean_reversion_signals_asymmetric(
        closes, 20, long_threshold=2.75, short_threshold=2.0, exit_sigma=0.1
    )
    assert actions[-1] != LONG


def test_mean_reversion_empty_window():
    strategy = MeanReversionSpyStrategy()
    assert strategy.evaluate_live(RollingWindow(10), {}) is None


def test_compute_atr_numba():
    n = 20
    highs = np.full(n, 101.0)
    lows = np.full(n, 99.0)
    closes = np.full(n, 100.0)
    assert compute_atr(highs, lows, closes, 14) == 2.0


def test_mean_reversion_action_codes():
    assert mean_reversion_action(80.0, 100.0, 5.0, 1.5, 0.1) == LONG
