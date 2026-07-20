"""
GD-S9: cross-stream singleton state must not bleed across streams or symbols.

Pre-fix:
  - FeedStreamHealthRegistry had a single mode/timer — stock bar health masked
    a disconnected crypto stream.
  - FeedSequenceGuard._misalignment_events was a global int — misalignments on
    one symbol polluted the count for unrelated symbols.
  - IngestCircuitBreaker used global consecutive-breach counters — a slow BTC
    bar tripped the circuit for QQQ.

Post-fix: all three carry state per-stream-type or per-(symbol, timeframe).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from src.ingestor.feed_stream_health import FeedStreamHealthRegistry, FeedStreamMode
from src.ingestor.feed_ingest_guard import (
    FeedSequenceGuard,
    IngestCircuitBreaker,
    INGEST_ARRIVAL_LAG_CEILING_SECONDS,
    INGEST_CIRCUIT_BREAKER_STREAK,
)


# ---------------------------------------------------------------------------
# Test 1: stock stream bar must not clear a crypto stream disconnect
# ---------------------------------------------------------------------------

def test_stock_bar_does_not_mask_crypto_disconnect() -> None:
    """GD-S9: note_stream_bar('stock') must not flip the crypto stream to HEALTHY."""
    reg = FeedStreamHealthRegistry()
    # Disconnect crypto
    reg.note_disconnect("crypto")
    assert reg.mode("crypto") == FeedStreamMode.RECONNECTING

    # Stock bar arrives — must not affect crypto state
    reg.note_stream_bar("stock")

    assert reg.mode("stock") == FeedStreamMode.HEALTHY, (
        "GD-S9: stock stream is not healthy after a bar"
    )
    assert reg.mode("crypto") == FeedStreamMode.RECONNECTING, (
        "GD-S9: note_stream_bar('stock') incorrectly cleared crypto disconnect"
    )


# ---------------------------------------------------------------------------
# Test 2: per-symbol sequence misalignment counter
# ---------------------------------------------------------------------------

def test_sequence_misalignment_isolated_per_symbol() -> None:
    """GD-S9: misalignments on symbol A must not appear in symbol B's count."""
    guard = FeedSequenceGuard()
    t0 = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2024, 1, 2, 10, 15, tzinfo=timezone.utc)

    # Accept a bar for QQQ
    guard.evaluate("QQQ", "15Min", t1)
    # Force a misalignment on QQQ by re-submitting an older timestamp
    guard.evaluate("QQQ", "15Min", t0)

    assert guard.misalignment_count("QQQ", "15Min") == 1, (
        "GD-S9: misalignment count for QQQ should be 1"
    )
    assert guard.misalignment_count("SPY", "15Min") == 0, (
        "GD-S9: misalignment on QQQ bled into SPY count"
    )
    assert guard.misalignment_count("BTC/USD", "1Hour") == 0, (
        "GD-S9: misalignment on QQQ bled into BTC/USD count"
    )


# ---------------------------------------------------------------------------
# Test 3: per-symbol circuit breaker — BTC lag must not trip QQQ
# ---------------------------------------------------------------------------

def test_circuit_breaker_isolated_per_symbol() -> None:
    """GD-S9: late BTC bars must not trip the circuit breaker for QQQ."""
    breaker = IngestCircuitBreaker()

    # Submit INGEST_CIRCUIT_BREAKER_STREAK late bars for BTC/USD
    # Use a very old timestamp so lag >> ceiling
    old_ts = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for _ in range(INGEST_CIRCUIT_BREAKER_STREAK):
        breaker.note_bar_arrival(old_ts, "BTC/USD", "1Hour")

    assert breaker.is_tripped("BTC/USD", "1Hour"), (
        "GD-S9: BTC/USD circuit breaker should be tripped after repeated lag breaches"
    )
    assert not breaker.is_tripped("QQQ", "15Min"), (
        "GD-S9: BTC/USD breach incorrectly tripped QQQ circuit breaker"
    )


# ---------------------------------------------------------------------------
# Test 4: supervisor-level disconnect clears BOTH streams (regression)
# ---------------------------------------------------------------------------

def test_supervisor_disconnect_affects_all_streams() -> None:
    """GD-S9 regression: note_disconnect(None) from the supervisor must
    mark both stock and crypto as RECONNECTING (whole event loop restarted).
    """
    reg = FeedStreamHealthRegistry()
    # Both start healthy; simulate a healthy stock bar
    reg.note_stream_bar("stock")
    assert reg.mode("stock") == FeedStreamMode.HEALTHY

    # Supervisor-level disconnect (no stream_type)
    reg.note_disconnect()

    assert reg.mode("stock") == FeedStreamMode.RECONNECTING, (
        "GD-S9 regression: supervisor disconnect did not affect stock stream"
    )
    assert reg.mode("crypto") == FeedStreamMode.RECONNECTING, (
        "GD-S9 regression: supervisor disconnect did not affect crypto stream"
    )
