"""H2/H4: the freshness gate, re-specced on the CORRECT premise.

Once evaluation is bar-close-triggered (H1b) and the signal window is closed-only (H1a), the
freshness gate's only question is: "has a new bar CLOSED, and did we receive it?" — measured
from the bar's CLOSE (open + period), threshold 65s post-close delivery latency, NO bar_period
floor. These tests pin:
  - a freshly-delivered closed bar PASSES; a genuinely missed bar REJECTS
  - there is NO flapping across a simulated RTH session of consecutive closed bars
  - microstructure_guard is EXPLICITLY EXEMPT from the shared measurement (documented here)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.engine.bar_freshness import bar_staleness_seconds
from src.engine.cold_start_gate import (
    DEFAULT_MAX_BAR_AGE_SECONDS,
    bar_period_seconds_for,
    wall_clock_freshness_ok,
)

PERIOD = 900.0  # 15Min
T0 = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)


def test_freshly_closed_bar_passes_missed_bar_rejects():
    """A bar that closed 30s ago is fresh (PASS, <65s); a bar one full period unseen is stale."""
    bar_open = T0
    bar_close = bar_open + timedelta(seconds=PERIOD)

    # delivered 30s after close
    now = bar_close + timedelta(seconds=30)
    fresh, lag = wall_clock_freshness_ok(
        bar_open, bar_period_seconds=PERIOD, now=now, max_age_seconds=DEFAULT_MAX_BAR_AGE_SECONDS)
    assert fresh is True and abs(lag - 30.0) < 1.0

    # the next bar never arrived: this bar is now ~one full period past its close
    now_missed = bar_close + timedelta(seconds=PERIOD)
    stale, lag2 = wall_clock_freshness_ok(
        bar_open, bar_period_seconds=PERIOD, now=now_missed, max_age_seconds=DEFAULT_MAX_BAR_AGE_SECONDS)
    assert stale is False and lag2 > DEFAULT_MAX_BAR_AGE_SECONDS


def test_max_bar_age_is_period_relative_delivery_latency():
    """T1 (supersedes the flat-65 G2 form): the threshold is PERIOD + the 65s post-close delivery
    latency, so a 1Hour leg's naturally-old latest closed bar is not mis-flagged stere mid-hour."""
    from src.engine.cold_start_gate import ColdStartGateConfig, max_bar_age_for_timeframe

    cfg = ColdStartGateConfig()  # max_bar_age_seconds default 65
    assert max_bar_age_for_timeframe("15Min", cfg) == 900.0 + 65.0
    assert max_bar_age_for_timeframe("1Hour", cfg) == 3600.0 + 65.0
    assert max_bar_age_for_timeframe("4Hour", cfg) == 4.0 * 3600.0 + 65.0


def test_no_flapping_across_a_full_rth_session():
    """Across 26 consecutive 15Min bars, each delivered 30s after its close, the freshness
    reading AT EACH new-closed-bar moment is always PASS. The old open-measured gate flapped
    70s->930s->70s every bar; the close-measured gate does not."""
    for i in range(26):
        bar_open = T0 + timedelta(seconds=PERIOD * i)
        bar_close = bar_open + timedelta(seconds=PERIOD)
        now = bar_close + timedelta(seconds=30)   # delivered promptly
        fresh, lag = wall_clock_freshness_ok(
            bar_open, bar_period_seconds=PERIOD, now=now, max_age_seconds=DEFAULT_MAX_BAR_AGE_SECONDS)
        assert fresh is True, f"bar {i} flapped to STALE at lag={lag}"
        assert lag < DEFAULT_MAX_BAR_AGE_SECONDS


def test_staleness_measured_from_close_not_open():
    """bar_staleness_seconds clamps a not-yet-closed (forming) bar to 0, and reads delivery
    latency for a just-closed bar — the ONE shared measurement."""
    bar_open = T0
    # still forming (now < close) -> 0
    assert bar_staleness_seconds(bar_open, PERIOD, bar_open + timedelta(seconds=100)) == 0.0
    # closed 40s ago -> 40
    now = bar_open + timedelta(seconds=PERIOD + 40)
    assert abs(bar_staleness_seconds(bar_open, PERIOD, now) - 40.0) < 1e-6


def test_bar_period_seconds_for_known_timeframes():
    assert bar_period_seconds_for("15Min") == 900.0
    assert bar_period_seconds_for("1Hour") == 3600.0


def test_microstructure_guard_is_explicitly_exempt_from_shared_measurement():
    """G3d NAMED EXEMPTION — microstructure_guard.py:322 computes bar age from the bar
    timestamp directly (now - bar_ts), NOT via bar_freshness.bar_staleness_seconds, and is
    LEFT UNROUTED on purpose. Reason: it feeds a stale-QUOTE / book-pressure detector, not an
    entry-blocking freshness gate, and it operates on the live (forming) quote where measuring
    from a bar CLOSE would be meaningless. It has no bar_period in scope. This test records the
    exemption so a future 'route every staleness site through the shared helper' sweep does not
    silently absorb it without a decision."""
    import inspect

    from src.engine import microstructure_guard

    src = inspect.getsource(microstructure_guard)
    # it intentionally does NOT import the shared helper
    assert "bar_staleness_seconds" not in src, (
        "microstructure_guard now references the shared helper — the G3d exemption above must "
        "be re-adjudicated (is it now an entry-blocking freshness check?)."
    )
