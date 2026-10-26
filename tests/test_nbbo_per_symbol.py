"""U1 — per-symbol NBBO health, session-aware.

Two halves; fixing only one moves the bug:
  U1a — the nbbo rate was ONE shared deque, so BTC's verdict was computed from equity data
        (crypto_nbbo_snapshot_failed=0 for BTC, yet it HARD-breached on nbbo_fetch_critical).
  U1b — a None quote from a CLOSED equity market is NOT a failure — record NO sample. Fixing only
        U1a would then make the equity leg HARD-breach itself every night on its own per-symbol rate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.engine.slo_monitor import IntegritySeverity, SLOMonitor

ET = ZoneInfo("America/New_York")
RTH = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)          # 11:00 ET, market open
CLOSED = datetime(2026, 6, 24, 2, 0, tzinfo=timezone.utc)        # 02:00 ET, market closed


def test_closed_equity_none_records_no_sample_and_btc_untouched():
    m = SLOMonitor()
    for _ in range(10):
        m.note_nbbo_fetch(True, symbol="BTC/USD", asset_class="crypto")
    # equity NBBO returns None AFTER the close, many times -> NO sample recorded (U1b)
    for _ in range(50):
        m.note_nbbo_fetch(False, symbol="QQQ", asset_class="stock", reference_ts=CLOSED)
    assert m.nbbo_success_rate("QQQ") == 1.0        # no samples -> default healthy, not 0.0
    assert m.nbbo_success_rate("BTC/USD") == 1.0    # BTC's rate is UNTOUCHED (per-symbol, U1a)


def test_equity_failure_during_rth_drops_only_equity_rate():
    m = SLOMonitor()
    for _ in range(10):
        m.note_nbbo_fetch(True, symbol="BTC/USD", asset_class="crypto")
    for _ in range(20):
        m.note_nbbo_fetch(False, symbol="QQQ", asset_class="stock", reference_ts=RTH)  # genuine RTH failure
    assert m.nbbo_success_rate("QQQ") < 0.55        # the EQUITY rate dropped
    assert m.nbbo_success_rate("BTC/USD") == 1.0    # BTC is UNAFFECTED


def test_btc_genuine_failure_drops_btc_and_breaches():
    """The sensor still works — we narrowed the WRONG trigger, not all triggers."""
    m = SLOMonitor()
    for _ in range(20):
        m.note_nbbo_fetch(False, symbol="BTC/USD", asset_class="crypto")   # BTC's own nbbo genuinely fails
    assert m.nbbo_success_rate("BTC/USD") < 0.55
    now = RTH
    verdict = m.evaluate_data_integrity({
        "exchange_reference_ts": now, "asset_class": "crypto", "timeframe": "1Hour",
        "symbol": "BTC/USD",
        "latest_bar_timestamp": now - timedelta(minutes=30),
        "bar_timestamps": [now - timedelta(hours=i) for i in range(6)],
        # no nbbo_success_rate in payload -> reads the per-symbol rate
        "rl_backfill_lag_hours": 0.0,
    })
    assert verdict.severity == IntegritySeverity.HARD_BREACH
    assert "nbbo_fetch_critical" in verdict.reasons


def test_overnight_equity_records_no_samples_and_does_not_breach():
    m = SLOMonitor()
    # a full overnight window: the equity NBBO returns None every poll -> zero samples recorded
    t = CLOSED
    for _ in range(200):
        m.note_nbbo_fetch(False, symbol="SPY", asset_class="stock", reference_ts=t)
        t += timedelta(seconds=30)
        if t.astimezone(ET).hour >= 4:   # stop before pre-market
            break
    assert m.nbbo_success_rate("SPY") == 1.0  # no samples -> never breaches on its own rate overnight
