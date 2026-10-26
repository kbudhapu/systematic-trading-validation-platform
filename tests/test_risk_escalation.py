"""Uniform hierarchical risk escalation semantics."""

from __future__ import annotations

import pytest

from src.engine.engine_preemption import (
    EnginePreemptionLatch,
    EnginePreemptedException,
    PreemptionCheckpoint,
    RiskEscalationEngine,
    RiskEscalationLevel,
    parse_escalation_level,
)


def test_parse_escalation_level_requires_explicit_field() -> None:
    with pytest.raises(ValueError, match="escalation_level is required"):
        parse_escalation_level({})
    assert (
        parse_escalation_level({"escalation_level": "ENTRY_GATE_HALT"})
        == RiskEscalationLevel.ENTRY_GATE_HALT
    )


def test_escalation_transition_emits_level_changed() -> None:
    import structlog

    structlog.configure(
        processors=[structlog.processors.KeyValueRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )
    engine = RiskEscalationEngine()
    engine.transition(
        RiskEscalationLevel.ENTRY_GATE_HALT,
        commanded_by="test",
    )
    engine.transition(
        RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
        commanded_by="test",
    )
    snap = engine.snapshot()
    assert snap.level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
    assert snap.portfolio_liquidation_commanded is True
    assert snap.engine_shutdown_latched is True


def test_entry_gate_blocks_entries_without_portfolio_liquidation() -> None:
    engine = RiskEscalationEngine()
    engine.transition(RiskEscalationLevel.ENTRY_GATE_HALT)
    assert engine.blocks_all_entries() is True
    assert engine.should_flatten_portfolio() is False
    assert engine.is_engine_shutdown_latched() is False


def test_strategy_liquidate_preempts_only_target() -> None:
    engine = RiskEscalationEngine()
    engine.transition(
        RiskEscalationLevel.STRATEGY_LIQUIDATE,
        strategy_id="leg_a",
        symbol="QQQ",
    )
    assert engine.requires_preemption(strategy_id="leg_a", symbol="QQQ") is True
    assert engine.requires_preemption(strategy_id="leg_b", symbol="SPY") is False
    assert engine.blocks_entries_for_strategy("leg_a") is True
    assert engine.blocks_entries_for_strategy("leg_b") is False


def test_latch_should_preempt_respects_strategy_scope() -> None:
    from src.control.command_queue import (
        ControlCommand,
        ControlCommandStatus,
        ControlCommandType,
    )

    latch = EnginePreemptionLatch()
    command = ControlCommand(
        command_id="cmd-1",
        command_type=ControlCommandType.ENGAGE_KILL_SWITCH,
        payload={
            "escalation_level": "STRATEGY_LIQUIDATE",
            "strategy_id": "leg_a",
            "symbol": "QQQ",
        },
        status=ControlCommandStatus.PROCESSING,
        requested_by="test",
        idempotency_key=None,
        error_message=None,
        created_at="2026-01-01T00:00:00+00:00",
        processed_at=None,
    )
    latch.arm_command(
        command,
        RiskEscalationLevel.STRATEGY_LIQUIDATE,
        strategy_id="leg_a",
        symbol="QQQ",
    )
    assert latch.should_preempt(strategy_id="leg_a", symbol="QQQ") is True
    assert latch.should_preempt(strategy_id="leg_b", symbol="SPY") is False


def test_preempted_exception_carries_escalation_level() -> None:
    exc = EnginePreemptedException(
        PreemptionCheckpoint(phase="phase_c", step="execute:leg_a"),
        escalation_level=RiskEscalationLevel.STRATEGY_LIQUIDATE,
    )
    assert exc.escalation_level == RiskEscalationLevel.STRATEGY_LIQUIDATE
