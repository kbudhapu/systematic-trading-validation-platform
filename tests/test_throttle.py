"""F5 (M3-RED-2) — ThrottledNotifier: dedup + exponential backoff + recovered.

A repeating condition (stale heartbeat every 30s poll) must NOT page every time -- that trains the
operator to ignore the channel and burns the ntfy rate limit so real alerts drop. The push channel
dedups by (component, kind) and backs off: immediate, then 5m, 15m, 60m, then hourly. When the
condition clears, exactly ONE 'recovered' page fires and the state resets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.alerts.throttle import ThrottledNotifier


class _Spy:
    def __init__(self):
        self.alerts = []

    def notify(self, alert):
        self.alerts.append(alert)


class _Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


def _alert(component="soak", kind="heartbeat_stale"):
    return {"kind": kind, "severity": "critical", "message": f"{component} {kind}",
            "detail": {"component": component, "age_seconds": 130}}


def _fresh():
    return _Clock(datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc))


def test_first_alert_sends_immediately():
    spy = _Spy()
    ThrottledNotifier(spy, clock=_fresh()).notify(_alert())
    assert len(spy.alerts) == 1


def test_repeat_within_backoff_is_suppressed():
    spy, clk = _Spy(), _fresh()
    t = ThrottledNotifier(spy, clock=clk)
    t.notify(_alert())                 # send #1 (immediate)
    clk.advance(30)
    t.notify(_alert())                 # 30s later: within 5m -> suppressed
    clk.advance(60)
    t.notify(_alert())                 # 90s: still within 5m -> suppressed
    assert len(spy.alerts) == 1


def test_backoff_schedule_progression():
    """Sends allowed at t0, +5m, +15m, +60m, then hourly -- never faster."""
    spy, clk = _Spy(), _fresh()
    t = ThrottledNotifier(spy, clock=clk)
    t.notify(_alert())                 # #1 at t0
    clk.advance(299); t.notify(_alert())   # 4m59s: < 5m -> suppressed
    assert len(spy.alerts) == 1
    clk.advance(2);   t.notify(_alert())   # 5m01s: >= 5m -> send #2
    assert len(spy.alerts) == 2
    clk.advance(900); t.notify(_alert())   # +15m -> send #3
    assert len(spy.alerts) == 3
    clk.advance(899); t.notify(_alert())   # +14m59s: < 60m -> suppressed
    assert len(spy.alerts) == 3
    clk.advance(2701); t.notify(_alert())  # total 60m01s since #3 -> send #4
    assert len(spy.alerts) == 4
    clk.advance(3600); t.notify(_alert())  # hourly cadence -> send #5
    assert len(spy.alerts) == 5


def test_distinct_keys_are_independent():
    spy, clk = _Spy(), _fresh()
    t = ThrottledNotifier(spy, clock=clk)
    t.notify(_alert(component="soak"))
    t.notify(_alert(component="mirror_sync"))     # different component -> its own state
    t.notify(_alert(component="soak", kind="wal_backlog"))  # different kind
    assert len(spy.alerts) == 3


def test_resolve_sends_one_recovered_and_resets():
    spy, clk = _Spy(), _fresh()
    t = ThrottledNotifier(spy, clock=clk)
    t.notify(_alert())                             # firing
    t.resolve(component="soak", kind="heartbeat_stale")
    assert len(spy.alerts) == 2
    assert spy.alerts[1]["detail"]["recovered"] is True
    assert "RECOVERED" in spy.alerts[1]["message"]
    # state reset -> the next occurrence pages IMMEDIATELY again (no lingering backoff)
    clk.advance(1)
    t.notify(_alert())
    assert len(spy.alerts) == 3


def test_resolve_unknown_key_is_noop():
    spy = _Spy()
    ThrottledNotifier(spy, clock=_fresh()).resolve(component="soak", kind="heartbeat_stale")
    assert spy.alerts == []                        # nothing was firing -> no recovered spam
