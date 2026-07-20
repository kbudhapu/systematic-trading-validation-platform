"""Tests for PostgreSQL LISTEN/NOTIFY config push listener."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.engine.config_listener import (
    LISTEN_CHANNEL,
    LocalConfigDirtyFlag,
    SupabaseConfigListener,
    get_config_listener,
)


def test_local_config_dirty_flag_is_thread_safe() -> None:
    flag = LocalConfigDirtyFlag()
    assert flag.is_set is False
    flag.set()
    assert flag.is_set is True
    flag.clear()
    assert flag.is_set is False


def test_listener_marks_dirty_on_notify() -> None:
    listener = SupabaseConfigListener(postgres_settings=None)
    notify = MagicMock(channel=LISTEN_CHANNEL, payload='{"name":"mean_reversion_qqq"}')
    listener._handle_notify(notify)
    assert listener.local_config_dirty is True


def test_listener_skips_start_without_postgres() -> None:
    listener = SupabaseConfigListener(postgres_settings=None)
    listener.start()
    assert listener.is_running is False


def test_get_config_listener_returns_singleton() -> None:
    with patch("src.engine.config_listener._listener", None):
        first = get_config_listener()
        second = get_config_listener()
    assert first is second


def test_consume_config_push_gate_clears_dirty_flag() -> None:
    import asyncio
    from src.engine.orchestrator import TradingOrchestrator

    listener = SupabaseConfigListener(postgres_settings=None)
    listener.mark_local_config_dirty()
    orchestrator = object.__new__(TradingOrchestrator)
    orchestrator.config_listener = listener
    orchestrator.config_watcher = MagicMock()
    orchestrator.config_resolver = MagicMock()
    orchestrator._sync_leg_states = MagicMock()

    with patch.object(TradingOrchestrator, "reload_config") as reload_mock:
        asyncio.run(orchestrator._consume_config_push_gate())

    reload_mock.assert_called_once()
    assert listener.local_config_dirty is False


def test_consume_config_push_gate_noop_when_clean() -> None:
    import asyncio
    from src.engine.orchestrator import TradingOrchestrator

    listener = SupabaseConfigListener(postgres_settings=None)
    orchestrator = object.__new__(TradingOrchestrator)
    orchestrator.config_listener = listener
    orchestrator.config_watcher = MagicMock()
    orchestrator.config_resolver = MagicMock()
    orchestrator._sync_leg_states = MagicMock()

    asyncio.run(orchestrator._consume_config_push_gate())

    orchestrator.config_watcher.request_reload.assert_not_called()
    orchestrator.config_watcher.get_latest.assert_not_called()


def test_listener_liveness_probe_failure_raises() -> None:
    import sys

    listener = SupabaseConfigListener(
        postgres_settings=None,
        liveness_probe_idle_seconds=0.05,
        wait_timeout_seconds=0.02,
    )
    conn = MagicMock()
    conn.wait.return_value = False
    conn.notifies = []
    conn.cursor.side_effect = RuntimeError("socket closed")

    mock_psycopg = MagicMock()
    with patch.dict(sys.modules, {"psycopg": mock_psycopg, "psycopg.sql": mock_psycopg.sql}):
        with patch.object(listener, "_open_listen_connection", return_value=conn):
            with patch.object(listener, "_close_active_connection"):
                with pytest.raises(RuntimeError, match="socket closed"):
                    listener._consume_notifications()
