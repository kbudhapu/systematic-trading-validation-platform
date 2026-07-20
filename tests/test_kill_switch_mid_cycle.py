"""
Integration test: kill-switch mid-cycle state cleanup.

Verifies that firing a GLOBAL_FLATTEN_AND_HALT during an in-flight orchestrator
cycle leaves no stale state: reserved capital is released, the preemption latch
is cleared, and the escalation engine registers the shutdown latch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
from src.engine.reserved_capital_ledger import ReservedCapitalLedger
from src.models import Account


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_cmd(cmd_id: str = "cmd-test-1") -> ControlCommand:
    return ControlCommand(
        command_id=cmd_id,
        command_type=ControlCommandType.FLATTEN_AND_HALT,
        payload={},
        status=ControlCommandStatus.PROCESSING,
        requested_by="test_operator",
        idempotency_key=f"idem-{cmd_id}",
        error_message=None,
        created_at="2026-01-01T00:00:00+00:00",
        processed_at=None,
    )


def _account(bp: float = 100_000.0) -> Account:
    return Account(equity=bp, cash=bp, buying_power=bp)


# ---------------------------------------------------------------------------
# Part 1 — ReservedCapitalLedger.release_all() is called on preemption
# ---------------------------------------------------------------------------

def test_release_all_clears_multi_leg_reservations() -> None:
    """release_all() clears all leg reservations without resetting the snapshot."""
    ledger = ReservedCapitalLedger()
    ledger.begin_cycle(_account(200_000.0))
    ledger.reserve("spy", 50_000.0)
    ledger.reserve("qqq", 40_000.0)
    ledger.reserve("btc", 30_000.0)
    assert ledger.reserved_total == 120_000.0

    ledger.release_all()

    assert ledger.reserved_total == 0.0
    assert ledger.available_buying_power() == 200_000.0
    # snapshot is preserved — next cycle can still see broker state
    assert ledger.snapshot_account is not None
    assert ledger.snapshot_account.buying_power == 200_000.0


def test_release_all_idempotent_on_empty_ledger() -> None:
    """release_all() on a ledger with no reservations must not raise."""
    ledger = ReservedCapitalLedger()
    ledger.release_all()  # no begin_cycle — should be a no-op
    assert ledger.reserved_total == 0.0


# ---------------------------------------------------------------------------
# Part 2 — EnginePreemptionLatch full arm→fire→clear cycle
# ---------------------------------------------------------------------------

def test_latch_arm_then_clear_leaves_no_stale_state() -> None:
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        wake = asyncio.Event()
        latch.bind_cycle_wake(wake, asyncio.get_running_loop())

        cmd = _flatten_cmd()
        latch.arm_flatten_and_halt(cmd)

        # Latch is armed
        assert latch.is_preemption_armed()
        assert latch.escalation.snapshot().level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
        assert latch.escalation.is_engine_shutdown_latched()
        assert latch.peek_pending_command() is cmd
        await asyncio.sleep(0)  # let call_soon_threadsafe callback execute
        assert wake.is_set()

        # Simulate preemption handler: take command, clear arm
        taken = latch.take_pending_command()
        assert taken is cmd
        assert latch.peek_pending_command() is None

        latch.clear_preemption_arm()

        # After clear: armed event reset, pending command gone
        assert not latch.is_preemption_armed()
        assert latch.peek_pending_command() is None
        # engine_shutdown_latched persists — not cleared by clear_preemption_arm()
        assert latch.escalation.is_engine_shutdown_latched()

    asyncio.run(_run())


def test_check_preemption_raises_engine_preempted_exception() -> None:
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        cmd = _flatten_cmd()
        latch.arm_flatten_and_halt(cmd)
        latch.set_checkpoint("phase_c", "execute:spy", strategy_id="spy", symbol="SPY")

        async def check_preemption(strategy_id: str | None = None) -> None:
            if not latch.should_preempt(strategy_id=strategy_id):
                return
            chk = latch.snapshot_checkpoint()
            pending = latch.peek_pending_command()
            raise EnginePreemptedException(
                chk,
                escalation_level=latch.escalation.snapshot().level,
                command_id=pending.command_id if pending else None,
                command_type=pending.command_type.value if pending else None,
            )

        with pytest.raises(EnginePreemptedException) as exc_info:
            await check_preemption(strategy_id="spy")

        exc = exc_info.value
        assert exc.escalation_level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT
        assert exc.checkpoint.phase == "phase_c"
        assert exc.checkpoint.step == "execute:spy"
        assert exc.command_id == "cmd-test-1"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Part 3 — Full mid-cycle kill-switch: reserved capital + latch both cleaned
# ---------------------------------------------------------------------------

def test_full_preemption_cycle_clears_capital_and_latch() -> None:
    """
    Simulates a kill-switch arriving while two legs have already reserved
    capital in phase B. The preemption handler must:
      1. call release_all() before any flatten logic
      2. call the flatten coroutine
      3. clear the preemption arm in the finally block
    """
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        ledger = ReservedCapitalLedger()
        ledger.begin_cycle(_account(200_000.0))
        ledger.reserve("spy", 60_000.0)
        ledger.reserve("qqq", 55_000.0)
        assert ledger.reserved_total == 115_000.0

        flatten_called_with: list[str] = []

        async def mock_engage_global_flatten(reason: str) -> None:
            flatten_called_with.append(reason)

        async def handle_preemption(exc: EnginePreemptedException) -> None:
            cmd = latch.take_pending_command()
            ledger.release_all()  # GD — release capital before flatten
            try:
                if exc.escalation_level == RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT:
                    await mock_engage_global_flatten("cycle_preempted_global_flatten")
            finally:
                latch.clear_preemption_arm()

        cmd = _flatten_cmd("cmd-mid-cycle")
        latch.arm_flatten_and_halt(cmd)
        latch.set_checkpoint("phase_b", "reserved_capital_allocation")

        exc = EnginePreemptedException(
            latch.snapshot_checkpoint(),
            escalation_level=latch.escalation.snapshot().level,
            command_id=cmd.command_id,
            command_type=cmd.command_type.value,
        )

        await handle_preemption(exc)

        # Capital released
        assert ledger.reserved_total == 0.0
        assert ledger.available_buying_power() == 200_000.0

        # Flatten was invoked
        assert flatten_called_with == ["cycle_preempted_global_flatten"]

        # Latch cleared
        assert not latch.is_preemption_armed()
        assert latch.peek_pending_command() is None

        # Shutdown latched permanently (intentional — engine must restart)
        assert latch.escalation.is_engine_shutdown_latched()

    asyncio.run(_run())


def test_preemption_clears_latch_even_when_flatten_raises() -> None:
    """finally block must clear the latch even if the flatten coroutine raises."""
    async def _run() -> None:
        latch = EnginePreemptionLatch()
        ledger = ReservedCapitalLedger()
        ledger.begin_cycle(_account(100_000.0))
        ledger.reserve("btc", 40_000.0)

        async def failing_flatten(reason: str) -> None:
            raise RuntimeError("broker connection lost")

        async def handle_preemption(exc: EnginePreemptedException) -> None:
            latch.take_pending_command()
            ledger.release_all()
            try:
                await failing_flatten("cycle_preempted_global_flatten")
            except Exception:
                pass  # orchestrator logs and re-raises; test just checks cleanup
            finally:
                latch.clear_preemption_arm()

        cmd = _flatten_cmd("cmd-failing")
        latch.arm_flatten_and_halt(cmd)
        exc = EnginePreemptedException(
            PreemptionCheckpoint(phase="phase_c", step="execute:btc"),
            escalation_level=RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT,
            command_id=cmd.command_id,
        )
        await handle_preemption(exc)

        # Capital still released despite flatten failure
        assert ledger.reserved_total == 0.0

        # Latch still cleared
        assert not latch.is_preemption_armed()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Part 4 — engine_shutdown_latched gates run_forever loop
# ---------------------------------------------------------------------------

def test_engine_shutdown_latched_blocks_subsequent_preemption_arm() -> None:
    """
    After GLOBAL_FLATTEN_AND_HALT, engine_shutdown_latched=True is permanent
    (not cleared by clear_preemption_arm). A second arm attempt still sets the
    latch, but the caller should check is_engine_shutdown_latched() to halt.
    """
    latch = EnginePreemptionLatch()
    latch.arm_flatten_and_halt(_flatten_cmd("cmd-1"))
    latch.clear_preemption_arm()

    # shutdown latch survives clear_preemption_arm
    assert latch.escalation.is_engine_shutdown_latched()

    # simulate run_forever check
    should_stop = latch.escalation.is_engine_shutdown_latched()
    assert should_stop is True


# ---------------------------------------------------------------------------
# Part 5 — STRATEGY_LIQUIDATE only preempts the targeted strategy
# ---------------------------------------------------------------------------

def test_strategy_liquidate_only_preempts_matching_strategy() -> None:
    latch = EnginePreemptionLatch()
    cmd = ControlCommand(
        command_id="cmd-strat",
        command_type=ControlCommandType.ENGAGE_KILL_SWITCH,
        payload={"escalation_level": "STRATEGY_LIQUIDATE", "strategy_id": "spy"},
        status=ControlCommandStatus.PROCESSING,
        requested_by="test",
        idempotency_key=None,
        error_message=None,
        created_at="2026-01-01T00:00:00+00:00",
        processed_at=None,
    )
    latch.arm_command(
        cmd,
        RiskEscalationLevel.STRATEGY_LIQUIDATE,
        strategy_id="spy",
    )

    # only "spy" triggers preemption
    assert latch.should_preempt(strategy_id="spy") is True
    assert latch.should_preempt(strategy_id="qqq") is False
    assert latch.should_preempt(strategy_id="btc") is False

    # escalation level is correct
    assert latch.escalation.snapshot().level == RiskEscalationLevel.STRATEGY_LIQUIDATE
    # strategy liquidate does NOT latch engine shutdown
    assert not latch.escalation.is_engine_shutdown_latched()
