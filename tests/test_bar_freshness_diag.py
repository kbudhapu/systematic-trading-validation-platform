"""FD-B: lock the CORRECT freshness semantics the sensor must preserve — the 'truth' the
live bar_freshness_critical trip should reflect. The diagnosis (docs/audits/freshness_diagnosis_
2026-07-15.md) is that the live sensor reads ~2x the bar period on a HEALTHY feed; these tests
pin what a correct reading looks like so (a) a genuinely stale bar STILL trips (positive control)
and (b) a fresh bar reads ~0 while a bar one window behind reads ~1 period (latest-vs-truth)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.engine.bar_freshness import bar_staleness_seconds, stale_after_seconds
from src.engine.slo_monitor import (
    BAR_DELIVERY_LATENCY_HARD_SECONDS,
    BAR_DELIVERY_LATENCY_SOFT_SECONDS,
)

PERIOD = 900.0  # 15Min
NOW = datetime(2026, 7, 15, 19, 0, 0, tzinfo=timezone.utc)


def test_positive_control_genuinely_stale_bar_still_trips_critical():
    """A bar that opened 3 periods ago (closed 2 periods ago) is GENUINELY stale and MUST exceed
    the critical threshold — the fix must not blind the sensor to real staleness."""
    stale_open = NOW - timedelta(seconds=3 * PERIOD)
    staleness = bar_staleness_seconds(stale_open, PERIOD, NOW)   # ~2 periods since close
    assert staleness > stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_HARD_SECONDS), (
        f"a 2-period-stale bar must trip critical: {staleness} vs "
        f"{stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_HARD_SECONDS)}")


def test_latest_vs_truth_fresh_bar_reads_near_zero():
    """A freshly-CLOSED bar (opened one period ago, closing now) reads ~0 staleness — NOT one
    period. This is the truth the live 2x-period reading violates."""
    fresh_open = NOW - timedelta(seconds=PERIOD)                 # closes exactly at NOW
    staleness = bar_staleness_seconds(fresh_open, PERIOD, NOW)
    assert staleness < 1.0, f"a just-closed bar must read ~0, got {staleness}"
    # and it is WELL under even the soft threshold -> a healthy just-closed feed never drifts.
    assert staleness < stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_SOFT_SECONDS)


def test_latest_vs_truth_one_window_behind_reads_one_period():
    """A bar one window behind (opened 2 periods ago, closed 1 period ago) reads ~1 period —
    which is BELOW the critical threshold (period + hard latency). The live sensor reading ~2x
    period for this state is the overstatement the fix must remove."""
    behind_open = NOW - timedelta(seconds=2 * PERIOD)
    staleness = bar_staleness_seconds(behind_open, PERIOD, NOW)
    assert abs(staleness - PERIOD) < 1.0, f"one window behind must read ~1 period, got {staleness}"
    assert staleness < stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_HARD_SECONDS), (
        "one window behind must NOT trip critical (period + hard latency)")


def test_not_yet_closed_bar_clamps_to_zero():
    """A bar that has not closed yet is not 'stale' (clamped at 0)."""
    forming_open = NOW - timedelta(seconds=PERIOD / 2.0)         # mid-formation
    assert bar_staleness_seconds(forming_open, PERIOD, NOW) == 0.0


# ── MC-3 (2026-07-16): end-to-end through the EXCHANGE CLOCK — the pinned mechanism was the
# reference (verdict A: WS minute bars noted with the LEG period pushed exchange_reference one
# leg period forward), so these run the ACTUAL clock → staleness → threshold pipeline. ──────────

from src.ingestor.exchange_clock import ExchangeClockRegistry  # noqa: E402


def _reference_after_minute_bar(timeframe: str, minute_bar_open: datetime) -> datetime:
    registry = ExchangeClockRegistry()
    registry.note_stream_bar("QQQ", timeframe, bar_timestamp=minute_bar_open, asset_class="stock")
    return registry.reference_for("QQQ", timeframe, asset_class="stock")


def test_mc3_positive_control_stalled_closed_bars_still_trip_at_unchanged_threshold():
    """(i) POSITIVE CONTROL — closed bars genuinely STALLED (latest close 2+ periods ago) while
    minute bars keep arriving (clock advances with 'now'): freshness MUST still exceed the
    UNCHANGED critical threshold. The fix must not blind the sensor to real staleness."""
    stalled_open = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)   # closed 15:15
    minute_bar = datetime(2026, 7, 16, 17, 5, tzinfo=timezone.utc)     # 'now' ≈ 17:06
    reference = _reference_after_minute_bar("15Min", minute_bar)
    staleness = bar_staleness_seconds(stalled_open, PERIOD, reference)
    assert staleness > stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_HARD_SECONDS), (
        f"genuinely stalled bars must STILL trip critical: {staleness}")


def test_mc3_sensor_agrees_with_reconcile_truth_during_normal_append():
    """(ii) During NORMAL append (latest bar just closed, minute bar arriving now) the corrected
    reference sits within one minute of 'now' and the computed staleness is within ~delivery
    latency — i.e. within one bar period of the reconcile truth, never inflated by a period."""
    latest_open = datetime(2026, 7, 16, 16, 45, tzinfo=timezone.utc)   # closed 17:00
    minute_bar = datetime(2026, 7, 16, 17, 1, tzinfo=timezone.utc)     # arriving ≈ now 17:02
    reference = _reference_after_minute_bar("15Min", minute_bar)
    assert reference - minute_bar == timedelta(minutes=1)              # reference == exchange now
    staleness = bar_staleness_seconds(latest_open, PERIOD, reference)
    assert staleness <= PERIOD, f"sensor must agree with truth within one period, got {staleness}"
    assert staleness < stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_SOFT_SECONDS)


def test_mc3_yesterdays_live_false_critical_values_now_read_at_most_one_period():
    """(iii) Yesterday's live false-critical tuple (QQQ 1740s read on a 900s period): latest closed
    bar open 16:30 (end 16:45), minute bar 16:59. OLD reference = 16:59+15min = 17:14 → 1740s
    (HARD false-trip). CORRECTED reference = 16:59+1min = 17:00 → 900s = one period, NO trip."""
    latest_open = datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc)   # closed 16:45
    minute_bar = datetime(2026, 7, 15, 16, 59, tzinfo=timezone.utc)
    reference = _reference_after_minute_bar("15Min", minute_bar)
    staleness = bar_staleness_seconds(latest_open, PERIOD, reference)
    assert staleness == PERIOD, f"the 1740s false read must become one period (900s), got {staleness}"
    assert staleness <= stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_SOFT_SECONDS), (
        "yesterday's false-critical state must no longer trip even the SOFT threshold")
    assert staleness < stale_after_seconds(PERIOD, BAR_DELIVERY_LATENCY_HARD_SECONDS)
