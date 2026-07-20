"""Tests for forced liquidation cancel confirmation polling."""

from __future__ import annotations

import asyncio
from pathlib import Path

LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS = 5.0
LIQUIDATION_CANCEL_POLL_INTERVAL_SECONDS = 0.25

from src.engine.control_plane import ControlPlaneSupervisor, SupervisorState
from src.engine.degradation_manager import DegradationManager, OperationalMode
from src.engine.governance import ensure_governance_schema


class CancelAwaitBroker:
    def __init__(self, open_counts: list[int]) -> None:
        self._open_counts = list(open_counts)
        self.cancel_calls = 0

    async def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        self.cancel_calls += 1
        return 1

    async def count_open_orders_for_symbol(self, symbol: str) -> int:
        if self._open_counts:
            return int(self._open_counts.pop(0))
        return 1

    async def await_open_orders_cleared(
        self,
        symbol: str,
        *,
        timeout_seconds: float = LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
        poll_interval_seconds: float = LIQUIDATION_CANCEL_POLL_INTERVAL_SECONDS,
    ) -> tuple[bool, int]:
        deadline = asyncio.get_event_loop().time() + max(timeout_seconds, 0.0)
        symbol_key = symbol.upper()
        remaining = await self.count_open_orders_for_symbol(symbol_key)
        if remaining == 0:
            return True, 0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(max(poll_interval_seconds, 0.05))
            remaining = await self.count_open_orders_for_symbol(symbol_key)
            if remaining == 0:
                return True, 0
        return False, remaining


def test_await_open_orders_cleared_succeeds_when_orders_drop_to_zero() -> None:
    async def run() -> None:
        broker = CancelAwaitBroker([2, 1, 0])
        cleared, remaining = await broker.await_open_orders_cleared(
            "QQQ",
            timeout_seconds=1.0,
            poll_interval_seconds=0.05,
        )
        assert cleared is True
        assert remaining == 0

    asyncio.run(run())


def test_await_open_orders_cleared_times_out_with_remaining_orders() -> None:
    async def run() -> None:
        broker = CancelAwaitBroker([1, 1, 1, 1])
        cleared, remaining = await broker.await_open_orders_cleared(
            "QQQ",
            timeout_seconds=0.2,
            poll_interval_seconds=0.05,
        )
        assert cleared is False
        assert remaining == 1

    asyncio.run(run())


def test_control_plane_escalates_liquidation_race_failure(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.db"
    ensure_governance_schema(vault_path)
    degradation = DegradationManager()
    supervisor = ControlPlaneSupervisor(
        vault_path=vault_path,
        on_block_consumption=degradation.apply_soft_degrade,
    )
    supervisor.transition(SupervisorState.STARTING, reason="test")
    supervisor.transition(SupervisorState.RUNNING, reason="test")

    supervisor.escalate_liquidation_race_failure(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        remaining_open_orders=2,
        timeout_seconds=5.0,
    )

    assert supervisor.state == SupervisorState.DEGRADED
    assert degradation.current_state().mode == OperationalMode.SOFT_DEGRADE


def test_orchestrator_liquidation_race_applies_soft_degrade_when_no_supervisor() -> None:
    from src.engine.orchestrator import TradingOrchestrator

    async def run() -> None:
        orch = TradingOrchestrator.__new__(TradingOrchestrator)
        degradation = DegradationManager()
        orch.degradation_manager = degradation
        orch.sync = type(
            "SyncStub",
            (),
            {"log_system_event": staticmethod(lambda *args, **kwargs: None)},
        )()
        orch._pre_flight_supervisor = None

        class StuckBroker:
            async def await_open_orders_cleared(self, symbol: str, **kwargs) -> tuple[bool, int]:
                return False, 3

            async def recover_forced_liquidation_cancel_timeout(
                self, symbol: str, **kwargs
            ) -> tuple[bool, str]:
                return False, "non_exit_orders_blocking_liquidation"

        orch.broker = StuckBroker()
        cleared = await orch._await_liquidation_cancel_clearance(
            "QQQ",
            strategy_id="mean_reversion_qqq",
            position_side="long",
            position_qty=10.0,
        )
        assert cleared is False
        assert degradation.current_state().mode == OperationalMode.SOFT_DEGRADE

    asyncio.run(run())


def test_orchestrator_liquidation_race_recovers_with_force_flatten() -> None:
    from src.engine.orchestrator import TradingOrchestrator

    async def run() -> None:
        orch = TradingOrchestrator.__new__(TradingOrchestrator)
        degradation = DegradationManager()
        orch.degradation_manager = degradation
        orch.sync = type(
            "SyncStub",
            (),
            {"log_system_event": staticmethod(lambda *args, **kwargs: None)},
        )()
        orch._pre_flight_supervisor = None

        class RecoveryBroker:
            async def await_open_orders_cleared(self, symbol: str, **kwargs) -> tuple[bool, int]:
                return False, 1

            async def recover_forced_liquidation_cancel_timeout(
                self, symbol: str, **kwargs
            ) -> tuple[bool, str]:
                return True, "forced_cancel_and_market_flatten"

        orch.broker = RecoveryBroker()
        cleared = await orch._await_liquidation_cancel_clearance(
            "QQQ",
            strategy_id="mean_reversion_qqq",
            position_side="long",
            position_qty=12.0,
        )
        assert cleared is True
        assert degradation.current_state().mode == OperationalMode.NORMAL

    asyncio.run(run())
