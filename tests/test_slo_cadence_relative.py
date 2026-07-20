"""FIX-1 (registry SLO-001): cadence-relative arrival-lag SLO.

Arrival-lag breach is judged as a fraction of the bar period. A 15s lag on a 15Min bar keeps
its current severity (regression pin); a benign ~60s lag on a 1Hour bar (the RA-DROPLET-FEED
case) never reaches HARD; genuine over-cadence lag still HARDs; a lone circuit trip is SOFT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.engine.slo_monitor import IntegritySeverity, SLOMonitor
from src.ingestor.feed_ingest_guard import IngestCircuitBreaker, STREAM_BAR_PERIOD_SECONDS


def _equity(**overrides):
    latest = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    p = {
        "exchange_reference_ts": latest + timedelta(minutes=5),
        "asset_class": "stock", "timeframe": "15Min",
        "latest_bar_timestamp": latest,
        "bar_timestamps": [latest - timedelta(minutes=15 * i) for i in range(6)],
        "nbbo_success_rate": 0.95, "rl_backfill_lag_hours": 2.0,
    }
    p.update(overrides)
    return p


def _crypto_1h(**overrides):
    latest = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    p = {
        "exchange_reference_ts": latest + timedelta(minutes=5),
        "asset_class": "crypto", "timeframe": "1Hour",
        "latest_bar_timestamp": latest,
        "bar_timestamps": [latest - timedelta(hours=i) for i in range(6)],
        "nbbo_success_rate": 0.95, "rl_backfill_lag_hours": 2.0,
    }
    p.update(overrides)
    return p


def test_regression_15s_on_15min_stays_ok():
    # 15/900 = 1.667% == SOFT fraction, not above it -> OK, exactly as today
    v = SLOMonitor().evaluate_data_integrity(_equity(ingest_arrival_lag_seconds=15.0))
    assert v.severity == IntegritySeverity.OK


def test_regression_20s_soft_40s_hard_on_15min():
    m = SLOMonitor()
    assert m.evaluate_data_integrity(_equity(ingest_arrival_lag_seconds=20.0)).severity == IntegritySeverity.SOFT_BREACH
    assert m.evaluate_data_integrity(_equity(ingest_arrival_lag_seconds=40.0)).severity == IntegritySeverity.HARD_BREACH


def test_btc_60s_on_1h_never_hard():
    # RA-DROPLET-FEED: 60/3600 = 1.667% -> OK/SOFT, NEVER HARD
    v = SLOMonitor().evaluate_data_integrity(_crypto_1h(ingest_arrival_lag_seconds=60.0))
    assert v.severity != IntegritySeverity.HARD_BREACH


def test_genuine_sustained_lag_on_1h_still_hard():
    # 300/3600 = 8.3% >> HARD fraction (3.3%) -> still HARD (SLO not neutered)
    v = SLOMonitor().evaluate_data_integrity(_crypto_1h(ingest_arrival_lag_seconds=300.0))
    assert v.severity == IntegritySeverity.HARD_BREACH


def test_lone_circuit_trip_is_soft_not_hard():
    v = SLOMonitor().evaluate_data_integrity(
        _crypto_1h(ingest_circuit_breaker_tripped=True, ingest_arrival_lag_seconds=0.0))
    assert v.severity == IntegritySeverity.SOFT_BREACH


def _late_open(seconds_after_close: float):
    # W1: lag is measured from the WS bar's CLOSE (open + 60s WS cadence). A bar `s` seconds late
    # after its close therefore OPENED (60 + s)s ago.
    return datetime.now(timezone.utc) - timedelta(seconds=STREAM_BAR_PERIOD_SECONDS + seconds_after_close)


def test_guard_cadence_ceiling_and_per_key_isolation():
    cb = IngestCircuitBreaker()
    # BTC 1Hour: 3 arrivals ~40s after close (< 60s cadence ceiling) -> NOT tripped
    for _ in range(3):
        cb.note_bar_arrival(_late_open(40), "BTC/USD", "1Hour")
    assert not cb.is_tripped("BTC/USD", "1Hour")
    # BTC 1Hour: 3 arrivals ~90s after close (> 60s ceiling) -> tripped
    for _ in range(3):
        cb.note_bar_arrival(_late_open(90), "BTC/USD", "1Hour")
    assert cb.is_tripped("BTC/USD", "1Hour")
    # isolation: QQQ 15Min is unaffected by BTC's trip; ~5s-after-close arrivals -> not tripped
    for _ in range(3):
        cb.note_bar_arrival(_late_open(5), "QQQ", "15Min")
    assert not cb.is_tripped("QQQ", "15Min")
    # QQQ 15Min: 3 arrivals ~30s after close (> 15s ceiling) -> tripped, independently of BTC
    for _ in range(3):
        cb.note_bar_arrival(_late_open(30), "QQQ", "15Min")
    assert cb.is_tripped("QQQ", "15Min")


def test_ws_minute_cadence_does_not_register_as_lag():
    """W1 — THE FIX. A 1-min WS bar delivered right after its own close (the 60s sawtooth that ran
    forever) must read ~0 lag and NEVER trip, on a 1Hour leg OR a 15Min leg. Before the fix, lag was
    measured from the bar OPEN, so the benign 60s cadence read as 60s of 'lag' and tripped every tick."""
    cb = IngestCircuitBreaker()
    for _ in range(20):  # a healthy WS bar opens ~60s ago, arrives just after its close
        cb.note_bar_arrival(_late_open(0.01), "BTC/USD", "1Hour")
        cb.note_bar_arrival(_late_open(0.01), "QQQ", "15Min")
    assert cb.snapshot("BTC/USD", "1Hour").last_arrival_lag_seconds < 1.0
    assert cb.snapshot("QQQ", "15Min").last_arrival_lag_seconds < 1.0
    assert not cb.is_tripped("BTC/USD", "1Hour")
    assert not cb.is_tripped("QQQ", "15Min")


def test_real_delivery_lag_still_detected_via_slo():
    """W1d — we fixed WHAT it measures, not disabled it. A 1H bar arriving 3s after close PASSES; one
    arriving 90s after close is a genuine SOFT breach. Fed through the SLO on the guard's own value."""
    cb = IngestCircuitBreaker()
    cb.note_bar_arrival(_late_open(3), "BTC/USD", "1Hour")
    lag_3s = cb.snapshot("BTC/USD", "1Hour").last_arrival_lag_seconds
    assert 2.0 < lag_3s < 4.0
    assert SLOMonitor().evaluate_data_integrity(
        _crypto_1h(ingest_arrival_lag_seconds=lag_3s)).severity == IntegritySeverity.OK

    cb.note_bar_arrival(_late_open(90), "BTC/USD", "1Hour")
    lag_90s = cb.snapshot("BTC/USD", "1Hour").last_arrival_lag_seconds
    assert 88.0 < lag_90s < 92.0
    assert SLOMonitor().evaluate_data_integrity(
        _crypto_1h(ingest_arrival_lag_seconds=lag_90s)).severity == IntegritySeverity.SOFT_BREACH
