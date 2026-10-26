"""Tests for scoped telemetry export and heartbeat dead-man monitoring."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.control.alerts import IncidentType, SystemHeartbeatMonitor
from src.engine.audit_panel import GovernanceTelemetryProvider
from src.ingestor.dual_buffer_manager import (
    LOCKED_BLOCK_COUNT,
    DualBufferDataCoordinator,
    get_dual_buffer_telemetry,
)
from src.models import Bar
from src.persistence import db as persistence
from src.persistence.db_queue import (
    AsyncDBWriter,
    QueuedWrite,
    WriteKind,
    get_queue_telemetry,
)


def _bar(minute: int, *, close: float = 100.0) -> Bar:
    ts = datetime(2026, 1, 2, 15, minute, tzinfo=timezone.utc)
    return Bar(
        timestamp=ts,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=1_000.0,
        symbol="QQQ",
    )


@pytest.fixture
def telemetry_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "research_vault.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_policy_lifecycle_state (
                strategy_id TEXT PRIMARY KEY,
                symbol TEXT,
                execution_state TEXT,
                probation_clean_trading_days INTEGER DEFAULT 0,
                probation_started_at TEXT,
                eviction_lockout_until TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS regime_champions (
                symbol TEXT,
                regime TEXT,
                composite_score REAL,
                promoted_at TEXT,
                params_json TEXT
            );
            CREATE TABLE IF NOT EXISTS maintenance_job_ledger (
                job_id TEXT PRIMARY KEY,
                last_status TEXT,
                validation_passed INTEGER,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS human_override_registry (
                kill_level TEXT,
                scope_key TEXT,
                engaged_at TEXT,
                engaged_by TEXT,
                rationale_hash TEXT,
                active INTEGER
            );
            CREATE TABLE IF NOT EXISTS live_attribution_ledger (
                trade_id TEXT PRIMARY KEY,
                timestamp TEXT,
                pnl REAL,
                slippage_pct REAL
            );
            """
        )
    return db_path


@pytest.fixture
def wal_db(tmp_path: Path) -> Path:
    return tmp_path / "wal.db"


def test_queue_telemetry_reports_wal_backlog_depth(wal_db: Path) -> None:
    writer = AsyncDBWriter(db_path=str(wal_db))
    writer._append_write_ahead(
        QueuedWrite(
            kind=WriteKind.PORTFOLIO_CONSTRAINT,
            payload={"timestamp": datetime.now(timezone.utc).isoformat()},
            db_path=str(wal_db),
        )
    )
    writer._append_write_ahead(
        QueuedWrite(
            kind=WriteKind.PORTFOLIO_CONSTRAINT,
            payload={"timestamp": datetime.now(timezone.utc).isoformat()},
            db_path=str(wal_db),
        )
    )

    telemetry = writer.telemetry_snapshot()
    assert telemetry["wal_backlog_depth"] == 2
    assert telemetry["queue_depth"] == 2


def test_audit_panel_collects_runtime_telemetry(
    telemetry_db: Path,
    wal_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.persistence.db_queue.get_async_db_writer",
        lambda: AsyncDBWriter(db_path=str(wal_db)),
    )

    coordinator = DualBufferDataCoordinator()
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    active = [_bar(minute) for minute in range(LOCKED_BLOCK_COUNT)]
    shadow = list(active)
    shadow[-1] = _bar(LOCKED_BLOCK_COUNT - 1, close=101.25)
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(active)
        state.shadow.replace_bars(shadow)
        state.last_shadow_refresh_monotonic = time.monotonic() - 45.0
    coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )

    monkeypatch.setattr(
        "src.ingestor.dual_buffer_manager.get_dual_buffer_coordinator",
        lambda: coordinator,
    )

    writer = AsyncDBWriter(db_path=str(wal_db))
    for _ in range(3):
        writer._append_write_ahead(
            QueuedWrite(
                kind=WriteKind.PORTFOLIO_CONSTRAINT,
                payload={"timestamp": datetime.now(timezone.utc).isoformat()},
                db_path=str(wal_db),
            )
        )
    monkeypatch.setattr(
        "src.persistence.db_queue.get_async_db_writer",
        lambda: writer,
    )

    provider = GovernanceTelemetryProvider(db_path=telemetry_db, environment="paper")
    metrics = provider._collect_runtime_telemetry()

    assert metrics["wal_backlog_depth"] == 3
    assert metrics["shadow_matrix_lag_seconds"] is not None
    assert metrics["shadow_matrix_lag_seconds"] >= 45.0
    assert metrics["tape_latch_active"] is True
    assert "leg_a" in metrics["tape_divergent_strategy_ids"]


def test_dual_buffer_telemetry_snapshot_flags_latch_and_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = DualBufferDataCoordinator()
    monkeypatch.setattr(
        "src.ingestor.dual_buffer_manager.get_dual_buffer_coordinator",
        lambda: coordinator,
    )
    coordinator.register_leg("leg_a", "QQQ", "15Min", asset_class="stock")
    with coordinator._lock:
        coordinator._matrices[("QQQ", "15Min")].last_shadow_refresh_monotonic = (
            time.monotonic() - 12.5
        )
        coordinator._divergent_latches["leg_a"] = True

    telemetry = coordinator.telemetry_snapshot()
    assert telemetry["tape_latch_active"] is True
    assert telemetry["shadow_matrix_lag_seconds"] >= 12.5
    assert get_dual_buffer_telemetry()["tape_latch_active"] is True


def test_get_queue_telemetry_exposes_backlog_counter(
    wal_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = AsyncDBWriter(db_path=str(wal_db))
    monkeypatch.setattr(
        "src.persistence.db_queue.get_async_db_writer",
        lambda: writer,
    )
    assert get_queue_telemetry()["wal_backlog_depth"] == 0


def test_system_heartbeat_monitor_dead_man_stale_flag(heartbeat_db) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    persistence.set_engine_heartbeat_timestamp(stale, db_path=heartbeat_db)

    monitor = SystemHeartbeatMonitor(
        cycle_interval_seconds=60.0,
        db_path=heartbeat_db,
    )
    assert monitor.is_dead_man_stale() is True

    pages: list[str] = []

    async def _capture(incident_type, message, metadata=None, **kwargs):
        pages.append(
            incident_type.value
            if hasattr(incident_type, "value")
            else str(incident_type)
        )
        from src.control.alerts import PageDispatchResult

        return PageDispatchResult(
            delivered=False,
            channel="test",
            status_code=None,
            reason="test",
            incident_type=str(incident_type),
        )

    with patch("src.control.alerts.dispatch_critical_page", _capture):
        assert monitor.evaluate_dead_man_once() is True
    assert IncidentType.DEAD_MAN_HEARTBEAT.value in pages


@pytest.fixture
def heartbeat_db(tmp_path):
    db_path = tmp_path / "heartbeat.db"
    persistence.init_db(db_path)
    return db_path
