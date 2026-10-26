"""Tests for command bus, feed health, and surgical config reload."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.control.command_queue import (
    ControlCommandStatus,
    ControlCommandType,
    enqueue_control_command,
    ensure_control_commands_schema,
    fetch_pending_commands,
    mark_command_completed,
)
from src.ingestor.feed_stream_health import (
    FEED_RECONNECT_DEGRADED_SECONDS,
    FeedStreamMode,
    FeedStreamHealthRegistry,
    get_feed_stream_health_registry,
)


@pytest.fixture
def isolated_command_db(tmp_path, monkeypatch):
    db_path = tmp_path / "commands.db"
    monkeypatch.setattr("src.control.command_queue.DB_PATH", db_path)
    monkeypatch.setattr("src.control.command_queue.get_supabase", lambda: None)
    ensure_control_commands_schema(db_path)
    return db_path


def test_enqueue_and_fetch_pending_command(isolated_command_db) -> None:
    command = enqueue_control_command(
        ControlCommandType.RELOAD_CONFIG,
        requested_by="test",
    )
    assert command.status == ControlCommandStatus.PENDING
    pending = fetch_pending_commands()
    assert len(pending) == 1
    assert pending[0].command_id == command.command_id
    mark_command_completed(command.command_id)
    assert fetch_pending_commands() == []


def test_feed_health_recovers_on_bar() -> None:
    registry = FeedStreamHealthRegistry()
    registry.note_disconnect()
    assert registry.mode() == FeedStreamMode.RECONNECTING
    registry.note_stream_bar("stock")
    assert registry.mode("stock") == FeedStreamMode.HEALTHY


def test_feed_health_degrades_after_timeout(monkeypatch) -> None:
    registry = FeedStreamHealthRegistry()
    start = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: start)
    registry.note_disconnect()
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: start + FEED_RECONNECT_DEGRADED_SECONDS + 1.0,
    )
    assert registry.is_degraded_feed()
    assert registry.mode() == FeedStreamMode.DEGRADED_FEED


def test_surgical_reload_preserves_runtime_objects() -> None:
    from src.engine.degradation_manager import DegradationManager
    from src.engine.orchestrator import TradingOrchestrator
    from src.engine.slo_monitor import SLOMonitor

    orchestrator = object.__new__(TradingOrchestrator)
    orchestrator.config_watcher = MagicMock()
    orchestrator.config_watcher.get_latest.return_value = MagicMock(
        risk=MagicMock(),
        capacity_governor=MagicMock(
            equity_turnover_multiplier=3.0,
            max_participation_rate=0.01,
            min_participation_rate=0.0025,
        ),
        environment="paper",
        strategies=[],
    )
    orchestrator.router = MagicMock()
    orchestrator.router.risk_manager = MagicMock()
    orchestrator.capacity_governor = MagicMock()
    orchestrator.config_resolver = MagicMock()
    orchestrator.degradation_manager = DegradationManager()
    orchestrator.slo_monitor = SLOMonitor()
    orchestrator._market_data_stream_clock = MagicMock()
    orchestrator._market_data_stream_clock.is_running = True
    orchestrator._sync_leg_states = MagicMock()
    orchestrator._enabled_legs = MagicMock(return_value=[])
    orchestrator._parity_auditor = MagicMock()
    orchestrator._parity_auditor.audit_and_commit.side_effect = lambda cfg: cfg

    prior_degradation = orchestrator.degradation_manager
    prior_slo = orchestrator.slo_monitor

    TradingOrchestrator.reload_config(orchestrator)

    assert orchestrator.degradation_manager is prior_degradation
    assert orchestrator.slo_monitor is prior_slo
    orchestrator._market_data_stream_clock.start.assert_not_called()


def test_get_feed_stream_health_registry_singleton() -> None:
    assert get_feed_stream_health_registry() is get_feed_stream_health_registry()
