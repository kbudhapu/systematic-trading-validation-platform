"""H1a: the forming bar must NEVER reach the signal path.

A RollingWindow holds only CLOSED bars in its deque/ring buffers; the currently-forming
(in-progress, sub-period) bar lives in a separate slot reachable only through
`forming_bar()` / `latest_including_forming()`. These tests assert the isolation is
structural — a forming bar cannot appear in `latest()` or any `*_array()`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.rolling_window import RollingWindow
from src.models import Bar

T0 = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)


def _bar(minute: int, close: float, *, sym="QQQ") -> Bar:
    ts = T0 + timedelta(minutes=minute)
    return Bar(timestamp=ts, open=close, high=close + 1, low=close - 1,
               close=close, volume=100.0, symbol=sym)


def test_forming_bar_never_enters_closed_arrays_or_latest():
    w = RollingWindow(maxlen=64)
    # two CLOSED 15Min bars (14:00, 14:15)
    assert w.append(_bar(0, 100.0))
    assert w.append(_bar(15, 101.0))
    # a live sub-period forming bar arrives (14:16) — must NOT enter the signal path
    assert w.set_forming(_bar(16, 999.0)) == "set"

    # latest() and every array are CLOSED-ONLY: the 999.0 forming close is absent
    assert w.latest().timestamp == T0 + timedelta(minutes=15)
    assert w.latest().close == 101.0
    assert list(w.closes_array()) == [100.0, 101.0]
    assert 999.0 not in list(w.closes_array())
    assert len(w) == 2

    # the forming bar is reachable ONLY through the dedicated accessors
    assert w.forming_bar().close == 999.0
    assert w.latest_including_forming().close == 999.0


def test_forming_updates_in_place_and_promotes_on_close():
    w = RollingWindow(maxlen=64)
    w.append(_bar(0, 100.0))
    w.set_forming(_bar(1, 100.5))
    w.set_forming(_bar(2, 100.9))          # revise the forming bar
    assert w.forming_bar().close == 100.9
    assert list(w.closes_array()) == [100.0]   # still one closed bar

    # when the period closes, the authoritative CLOSED bar arrives via append; the stale
    # forming slot (older-or-equal timestamp) is dropped
    w.append(_bar(15, 101.0))
    w.set_forming(_bar(2, 100.9))          # a late tick for an already-closed period
    assert w.forming_bar() is None or w.forming_bar().timestamp > w.latest().timestamp
    assert list(w.closes_array()) == [100.0, 101.0]


def test_set_forming_rejects_stale_and_out_of_order():
    w = RollingWindow(maxlen=64)
    w.append(_bar(15, 101.0))
    assert w.set_forming(_bar(10, 50.0)) == "ignored"   # older than latest closed
    assert w.set_forming(_bar(20, 102.0)) == "set"
    assert w.set_forming(_bar(18, 103.0)) == "ignored"  # out-of-order vs forming


def test_clear_resets_forming():
    w = RollingWindow(maxlen=8)
    w.append(_bar(0, 100.0))
    w.set_forming(_bar(1, 100.5))
    w.clear()
    assert w.forming_bar() is None
    assert w.latest() is None
