"""Engine preemption latch and cooperative cycle abortion."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.control.command_queue import (
    ControlCommand,
    ControlCommandStatus,
    ControlCommandType,
)
from src.engine.engine_preemption import (
    EnginePreemptedException,
    EnginePreemptionLatch,
    PreemptionCheckpoint,
    RiskEscalationLevel,
)


def _flatten_command() -> ControlCommand:
    return ControlCommand(
        command_id="cmd-flatten-1",
        command_type=ControlCommandType.FLATTEN_AND_HALT,
        payload={},
        status=ControlCommandStatus.PROCESSING,
        requested_by="test",
        idempotency_key=None,
        error_message=None,
        created_at="2026-01-01T00:00:00+00:00",
        processed_at=None,
    )


def test_latch_arms_flatten_halt_from_background_thread() -> None:
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        wake = asyncio.Event()
        latch.bind_cycle_wake(wake, asyncio.get_running_loop())
        command = _flatten_command()
        latch.arm_flatten_and_halt(command)
        assert latch.is_preemption_armed() is True
        assert latch.escalation.snapshot().level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
        assert latch.peek_pending_command() is command
        await asyncio.sleep(0)
        assert wake.is_set()

    asyncio.run(_run())


def test_check_preemption_raises_when_armed() -> None:
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        latch.arm_flatten_and_halt(_flatten_command())
        latch.set_checkpoint("phase_a", "leg_eval:qqq")

        async def check_preemption() -> None:
            if not latch.should_preempt():
                return
            checkpoint = latch.snapshot_checkpoint()
            pending = latch.peek_pending_command()
            raise EnginePreemptedException(
                checkpoint,
                escalation_level=latch.escalation.snapshot().level,
                command_id=pending.command_id if pending else None,
                command_type=pending.command_type.value if pending else None,
            )

        with pytest.raises(EnginePreemptedException) as exc_info:
            await check_preemption()
        assert exc_info.value.checkpoint.phase == "phase_a"
        assert exc_info.value.escalation_level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT

    asyncio.run(_run())


def test_clear_resets_armed_state() -> None:
    latch = EnginePreemptionLatch()
    latch.arm_flatten_and_halt(_flatten_command())
    latch.clear_preemption_arm()
    assert latch.is_preemption_armed() is False
    assert latch.peek_pending_command() is None


def test_handle_cycle_preemption_invokes_global_flatten() -> None:
    async def _run() -> None:
        orchestrator = MagicMock()
        latch = EnginePreemptionLatch()
        latch.arm_flatten_and_halt(_flatten_command())
        orchestrator._engage_global_flatten_and_halt = AsyncMock()

        async def handle_cycle_preemption(exc: EnginePreemptedException) -> None:
            command = latch.take_pending_command()
            assert command is not None
            if exc.escalation_level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT:
                await orchestrator._engage_global_flatten_and_halt(
                    reason="cycle_preempted_global_flatten"
                )
            latch.clear_preemption_arm()

        exc = EnginePreemptedException(
            PreemptionCheckpoint(phase="phase_c", step="execute:leg_a"),
            escalation_level=RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
            command_id="cmd-flatten-1",
            command_type=ControlCommandType.FLATTEN_AND_HALT.value,
        )
        await handle_cycle_preemption(exc)
        orchestrator._engage_global_flatten_and_halt.assert_awaited_once()
        assert latch.is_preemption_armed() is False

    asyncio.run(_run())
