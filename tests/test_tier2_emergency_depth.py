"""Tier 2 emergency command interrupts and level-1 depth routing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.control.command_queue import (
    ControlCommandStatus,
    ControlCommandType,
    EMERGENCY_COMMAND_TYPES,
    enqueue_control_command_local,
    ensure_control_commands_schema,
    fetch_and_claim_emergency_commands,
    try_claim_command,
)
from src.control.emergency_command_listener import EMERGENCY_COMMAND_POLL_SECONDS
from src.engine.execution_adaptor import (
    DEPTH_CONFIDENCE_HIGH,
    DEPTH_CONFIDENCE_LOW,
    DEPTH_CONFIDENCE_UNAVAILABLE,
    evaluate_level1_depth_routing,
)
from src.ingestor.level1_depth_cache import Level1DepthQuote, reset_level1_depth_cache
from src.models import Side


@pytest.fixture
def isolated_command_db(tmp_path, monkeypatch):
    db_path = tmp_path / "commands.db"
    monkeypatch.setattr("src.control.command_queue.DB_PATH", db_path)
    monkeypatch.setattr("src.control.command_queue.get_supabase", lambda: None)
    ensure_control_commands_schema(db_path)
    return db_path


def test_emergency_command_types_include_flatten_and_kill() -> None:
    assert ControlCommandType.FLATTEN_AND_HALT in EMERGENCY_COMMAND_TYPES
    assert ControlCommandType.ENGAGE_KILL_SWITCH in EMERGENCY_COMMAND_TYPES
    assert ControlCommandType.RELOAD_CONFIG not in EMERGENCY_COMMAND_TYPES


def test_emergency_poll_interval_is_sub_minute() -> None:
    assert EMERGENCY_COMMAND_POLL_SECONDS < 60.0


def test_try_claim_command_is_exclusive(isolated_command_db) -> None:
    command = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT)
    assert try_claim_command(command.command_id) is True
    assert try_claim_command(command.command_id) is False


def test_fetch_and_claim_emergency_commands(isolated_command_db) -> None:
    flatten = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT)
    enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG)
    claimed = fetch_and_claim_emergency_commands()
    assert len(claimed) == 1
    assert claimed[0].command_id == flatten.command_id
    assert claimed[0].status == ControlCommandStatus.PROCESSING


def test_evaluate_level1_depth_routing_uses_stream_sizes() -> None:
    reset_level1_depth_cache()
    stream = Level1DepthQuote(
        symbol="QQQ",
        bid_price=100.0,
        ask_price=100.1,
        bid_size=500.0,
        ask_size=200.0,
        timestamp=datetime.now(timezone.utc),
        source="stream",
    )
    evaluation = evaluate_level1_depth_routing(
        symbol="QQQ",
        side=Side.BUY,
        stream_quote=stream,
        rest_snapshot=None,
    )
    assert evaluation.depth_confidence == DEPTH_CONFIDENCE_HIGH
    assert evaluation.force_aggressive_ioc is False
    assert evaluation.bid_size == 500.0
    assert evaluation.ask_size == 200.0
    assert evaluation.book_pressure > 0.0


def test_evaluate_level1_depth_routing_defensive_when_depth_missing() -> None:
    evaluation = evaluate_level1_depth_routing(
        symbol="QQQ",
        side=Side.SELL,
        stream_quote=None,
        rest_snapshot=None,
    )
    assert evaluation.depth_confidence == DEPTH_CONFIDENCE_UNAVAILABLE
    assert evaluation.force_aggressive_ioc is True


def test_evaluate_level1_depth_routing_defensive_when_thin() -> None:
    stream = Level1DepthQuote(
        symbol="QQQ",
        bid_price=100.0,
        ask_price=100.5,
        bid_size=10.0,
        ask_size=5.0,
        timestamp=datetime.now(timezone.utc),
        source="stream",
    )
    evaluation = evaluate_level1_depth_routing(
        symbol="QQQ",
        side=Side.BUY,
        stream_quote=stream,
        rest_snapshot=None,
    )
    assert evaluation.depth_confidence == DEPTH_CONFIDENCE_LOW
    assert evaluation.force_aggressive_ioc is True
