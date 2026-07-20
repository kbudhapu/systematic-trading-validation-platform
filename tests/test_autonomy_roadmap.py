"""Tests for autonomy roadmap issues 5-8."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.engine.degradation_manager import DegradationManager, OperationalMode
from src.engine.recon_recovery import (
    RECON_RETRY_BACKOFF_SECONDS,
    clear_deferred_pre_open_recon,
    is_deferred_pre_open_recon_scheduled,
    schedule_pre_open_full_recon,
)
from src.engine.slo_monitor import IntegritySeverity, SLOMonitor
from src.ingestor.feed_ingest_guard import (
    INGEST_ARRIVAL_LAG_CEILING_SECONDS,
    INGEST_CIRCUIT_BREAKER_STREAK,
    STREAM_BAR_PERIOD_SECONDS,
    FeedSequenceGuard,
    IngestCircuitBreaker,
)
from src.persistence.db_queue import (
    WAL_BACKLOG_HIGH_WATER,
    WAL_BURST_MAX_BATCHES_PER_INTERVAL,
    WAL_MAX_BATCHES_PER_BAR_INTERVAL,
    WalLeakyBucketDrainer,
)


def test_recon_degraded_manage_blocks_entries_allows_exits() -> None:
    manager = DegradationManager()
    state = manager.apply_recon_degraded_manage("pre_flight_exhausted")
    assert state.mode == OperationalMode.RECON_DEGRADED_MANAGE
    assert state.block_new_entries is True
    assert state.allow_exit_tracking is True
    assert state.ignore_model_signals is False


def test_recon_retry_backoff_schedule() -> None:
    assert RECON_RETRY_BACKOFF_SECONDS == (5.0, 15.0, 30.0, 60.0, 60.0)


def test_deferred_pre_open_recon_latch(tmp_path) -> None:
    db_path = tmp_path / "vault.db"
    assert is_deferred_pre_open_recon_scheduled(db_path) is False
    schedule_pre_open_full_recon("test_reason", db_path=db_path)
    assert is_deferred_pre_open_recon_scheduled(db_path) is True
    assert clear_deferred_pre_open_recon(db_path) is True
    assert is_deferred_pre_open_recon_scheduled(db_path) is False


def test_sequence_guard_rejects_duplicate_timestamp() -> None:
    guard = FeedSequenceGuard()
    ts = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)
    assert guard.evaluate("QQQ", "15Min", ts).accepted is True
    assert guard.evaluate("QQQ", "15Min", ts).accepted is False
    assert guard.misalignment_count() == 1


def test_ingest_circuit_breaker_trips_after_consecutive_lag() -> None:
    breaker = IngestCircuitBreaker()
    # W1: arrival lag is measured from the WS bar's CLOSE (open + STREAM_BAR_PERIOD_SECONDS), not its
    # open. A bar genuinely (ceiling+5)s late therefore opens (60 + ceiling + 5)s ago.
    stale = datetime.now(timezone.utc) - timedelta(
        seconds=STREAM_BAR_PERIOD_SECONDS + INGEST_ARRIVAL_LAG_CEILING_SECONDS + 5.0
    )
    for _ in range(INGEST_CIRCUIT_BREAKER_STREAK):
        breaker.note_bar_arrival(stale, "QQQ", "15Min")
    assert breaker.is_tripped("QQQ", "15Min") is True


def test_slo_monitor_flags_ingest_circuit_breaker() -> None:
    monitor = SLOMonitor()
    now = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)  # fixed in-RTH (T2e: stocks dormant off-RTH)
    verdict = monitor.evaluate_data_integrity(
        {
            "exchange_reference_ts": now,
            "asset_class": "stock",
            "timeframe": "15Min",
            "latest_bar_timestamp": now - timedelta(minutes=15),
            "bar_timestamps": [now - timedelta(minutes=15 * i) for i in range(4)],
            "ingest_circuit_breaker_tripped": True,
        }
    )
    # SLO-001 (cadence-relative SLO): a LONE circuit trip is SOFT (recoverable), not an
    # unconditional HARD -- HARD is reserved for genuine over-cadence lag or co-occurring
    # critical signals. See registry STANDING-SLOCADENCE.
    assert verdict.severity == IntegritySeverity.SOFT_BREACH
    assert "ingest_circuit_breaker_tripped" in verdict.reasons


def test_wal_drainer_scales_burst_on_backlog() -> None:
    normal = WalLeakyBucketDrainer(
        max_batches_per_interval=WAL_MAX_BATCHES_PER_BAR_INTERVAL,
        burst_max_batches_per_interval=WAL_BURST_MAX_BATCHES_PER_INTERVAL,
    )
    normal.update_backlog_depth(WAL_BACKLOG_HIGH_WATER - 1)
    for _ in range(WAL_MAX_BATCHES_PER_BAR_INTERVAL):
        assert normal.acquire(force=False) is True
    assert normal.acquire(force=False) is False

    burst = WalLeakyBucketDrainer(
        max_batches_per_interval=WAL_MAX_BATCHES_PER_BAR_INTERVAL,
        burst_max_batches_per_interval=WAL_BURST_MAX_BATCHES_PER_INTERVAL,
    )
    burst.update_backlog_depth(WAL_BACKLOG_HIGH_WATER)
    acquired = sum(1 for _ in range(WAL_BURST_MAX_BATCHES_PER_INTERVAL) if burst.acquire(force=False))
    assert acquired == WAL_BURST_MAX_BATCHES_PER_INTERVAL


def test_config_resolver_blocks_promotion_when_degraded() -> None:
    from src.engine.config_engine import ConfigurationPrecedenceResolver

    watcher = MagicMock()
    watcher.config_fetch_degraded = True
    watcher.remote_source_required = True
    watcher.config_staleness_seconds.return_value = 42.0
    resolver = ConfigurationPrecedenceResolver()
    assert resolver.blocks_policy_promotion(watcher) is True


def test_pre_flight_defer_latch_skips_lock_engagement(tmp_path) -> None:
    from src.engine.pre_flight_reconciliation import PreFlightReconciliationEngine
    from src.engine.governance import is_pre_flight_recon_locked

    engine = PreFlightReconciliationEngine(
        broker=MagicMock(),
        strategy_symbols={"leg": "QQQ"},
        change_journal=MagicMock(),
        vault_path=tmp_path / "vault.db",
        defer_latch=True,
    )
    with patch(
        "src.engine.pre_flight_reconciliation.engage_pre_flight_recon_lock"
    ) as engage_mock:
        result = engine._latch_soft_degrade("test", metadata={})
    engage_mock.assert_not_called()
    assert result.latched_soft_degrade is True
    locked, _ = is_pre_flight_recon_locked(tmp_path / "vault.db")
    assert locked is False
