"""
Tests for RollingWindow ring-buffer wraparound correctness and bootstrap-then-reconcile.

The ring buffer uses a `_head` write pointer that advances modulo `maxlen`.
`_ordered()` reconstructs chronological order via np.concatenate when full.
These tests verify that close prices returned by `closes_array()` are always
the most-recent `maxlen` bars in chronological order, even across multiple
wraparound cycles.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.core.rolling_window import RollingWindow
from src.models import Bar


def _bar(close: float, minute_offset: int) -> Bar:
    return Bar(
        timestamp=datetime(2026, 6, 27, 9, 30, tzinfo=timezone.utc)
        + timedelta(minutes=minute_offset),
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=10_000.0,
        symbol="QQQ",
    )


def test_ring_buffer_wraparound_chronological_order() -> None:
    """
    After appending more bars than maxlen, closes_array() must return exactly
    the most recent maxlen bars in chronological order.  Verified across two
    full wraparound cycles.
    """
    maxlen = 10
    window = RollingWindow(maxlen=maxlen)

    # First pass: fill window completely
    total_bars = maxlen + 7  # one full wrap + 7 extra
    for i in range(total_bars):
        result = window.append(_bar(close=float(100 + i), minute_offset=i))
        assert result is True

    assert len(window) == maxlen
    closes = window.closes_array()
    assert len(closes) == maxlen

    # The most recent `maxlen` bars have closes 100+7 … 100+16
    expected = np.array([float(100 + i) for i in range(total_bars - maxlen, total_bars)])
    np.testing.assert_array_equal(closes, expected)

    # Second wraparound: add another maxlen bars on top
    for i in range(total_bars, total_bars + maxlen):
        window.append(_bar(close=float(100 + i), minute_offset=i))

    assert len(window) == maxlen
    closes2 = window.closes_array()
    assert len(closes2) == maxlen
    expected2 = np.array(
        [float(100 + i) for i in range(total_bars + maxlen - maxlen, total_bars + maxlen)]
    )
    np.testing.assert_array_equal(closes2, expected2)

    # Deque and numpy must agree on the most recent bar's close
    assert window.latest().close == pytest.approx(closes2[-1])


def test_bootstrap_then_reconcile_preserves_order() -> None:
    """
    Simulates the bootstrap sequence:
      1. Pre-load more REST bars than maxlen into a fresh window via append().
      2. Apply newer streaming bars via upsert_bar() (the reconcile step).

    After reconcile, closes_array() must be exactly the most recent maxlen
    bars (REST + streaming) in chronological order, with the streaming bar
    at the tail.
    """
    maxlen = 10
    window = RollingWindow(maxlen=maxlen)

    # Step 1: load REST bars — more than maxlen
    rest_count = maxlen + 5
    for i in range(rest_count):
        window.append(_bar(close=float(200 + i), minute_offset=i))

    assert len(window) == maxlen

    # Step 2: reconcile — one newer streaming bar arrives
    streaming_minute = rest_count  # strictly after all REST bars
    streaming_close = 999.0
    result = window.upsert_bar(_bar(close=streaming_close, minute_offset=streaming_minute))
    assert result == "appended"

    assert len(window) == maxlen

    closes = window.closes_array()
    assert len(closes) == maxlen

    # The streaming bar must be at the tail
    assert closes[-1] == pytest.approx(streaming_close)

    # The preceding bars must be the last (maxlen-1) REST bars in order
    # REST bars: closes 200..200+(rest_count-1); we want the last (maxlen-1) of those
    expected_rest_closes = [
        float(200 + i) for i in range(rest_count - (maxlen - 1), rest_count)
    ]
    np.testing.assert_array_equal(closes[:-1], np.array(expected_rest_closes))
