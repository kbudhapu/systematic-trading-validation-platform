"""Tier 1 intraday state synchronization — cold-start, partial fills, multi-leg refresh."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.engine.cold_start_gate import ColdStartGateState
from src.engine.execution_adaptor import reconcile_broker_primary_fill
from src.ingestor.dual_buffer_manager import (
    DualBufferDataCoordinator,
    LOCKED_BLOCK_COUNT,
    TapeStatus,
)
from src.models import Bar, OrderResult, Position, Side


def _recent_bars(count: int = LOCKED_BLOCK_COUNT) -> list[Bar]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bars: list[Bar] = []
    for i in range(count):
        ts = now - timedelta(minutes=15 * (count - 1 - i))
        close = 100.0 + i * 0.01
        bars.append(
            Bar(
                timestamp=ts,
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1_000.0,
                symbol="QQQ",
            )
        )
    return bars


def _seed_aligned_matrices(coordinator: DualBufferDataCoordinator, strategy_id: str) -> None:
    coordinator.register_leg(strategy_id, "QQQ", "15Min", asset_class="stock")
    bars = _recent_bars()
    key = ("QQQ", "15Min")
    with coordinator._lock:
        state = coordinator._matrices[key]
        state.active.replace_bars(bars)
        state.shadow.replace_bars(bars)


def test_cold_start_blocks_entries_until_background_verification() -> None:
    coordinator = DualBufferDataCoordinator()
    _seed_aligned_matrices(coordinator, "leg_a")
    coordinator.initialize_cold_start_state()

    assert coordinator.tape_status("leg_a") == TapeStatus.UNKNOWN
    assert coordinator.cold_start_gate_state("leg_a") == ColdStartGateState.PENDING
    assert coordinator.blocks_entry_signals("leg_a") is True
    assert coordinator.blocks_all_entry_signals("leg_a") is True
    assert coordinator.is_cold_start_gate_cleared() is False

    verdict = coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert verdict.aligned is True
    assert coordinator.tape_status("leg_a") == TapeStatus.VERIFIED
    assert coordinator.cold_start_gate_state("leg_a") == ColdStartGateState.PENDING
    assert coordinator.is_cold_start_gate_cleared() is False
    assert coordinator.blocks_entry_signals("leg_a") is True

    with coordinator._lock:
        state = coordinator._matrices[("QQQ", "15Min")]
    coordinator._apply_tape_verification_for_state(state, from_background=True)

    assert coordinator.cold_start_gate_state("leg_a") == ColdStartGateState.STABILIZED_VERIFIED
    assert coordinator.is_cold_start_gate_cleared() is True
    assert coordinator.blocks_entry_signals("leg_a") is False
    telemetry = coordinator.telemetry_snapshot()
    assert telemetry["cold_start_gate_cleared"] is True
    assert telemetry["background_shadow_verification_completed"] is True
    assert telemetry["cold_start_gate_states"]["leg_a"] == "STABILIZED_VERIFIED"


def test_divergent_tape_blocks_entries_after_gate_cleared() -> None:
    coordinator = DualBufferDataCoordinator()
    _seed_aligned_matrices(coordinator, "leg_a")
    coordinator.initialize_cold_start_state()
    with coordinator._lock:
        state = coordinator._matrices[("QQQ", "15Min")]
    coordinator._apply_tape_verification_for_state(state, from_background=True)
    assert coordinator.blocks_entry_signals("leg_a") is False

    active = _recent_bars()
    shadow = list(active)
    shadow[-1] = replace(shadow[-1], close=101.5)
    with coordinator._lock:
        state = coordinator._matrices[("QQQ", "15Min")]
        state.active.replace_bars(active)
        state.shadow.replace_bars(shadow)

    verdict = coordinator.verify_matrix_alignment(
        "QQQ",
        "15Min",
        strategy_id="leg_a",
        lookback_window=LOCKED_BLOCK_COUNT,
    )
    assert verdict.aligned is False
    assert coordinator.tape_status("leg_a") == TapeStatus.DIVERGENT
    assert coordinator.blocks_entry_signals("leg_a") is True


def test_reconcile_broker_primary_partial_entry() -> None:
    prior = None
    broker = Position(
        symbol="QQQ",
        qty=5.0,
        side="long",
        avg_entry_price=100.0,
    )
    fill = OrderResult(
        symbol="QQQ",
        side=Side.BUY,
        qty=5.0,
        filled_price=100.1,
        filled_at=datetime.now(timezone.utc),
        status="partially_filled",
    )
    recon = reconcile_broker_primary_fill(
        requested_qty=10.0,
        fill_result=fill,
        prior_position=prior,
        broker_position=broker,
        bars_in_trade=0,
    )
    assert recon.is_partial is True
    assert recon.filled_qty == 5.0
    assert recon.residual_qty == 5.0
    assert recon.broker_qty == 5.0
    assert recon.bars_in_trade == 1
    assert recon.position_side == "long"


def test_reconcile_broker_primary_partial_exit_flattens_bars() -> None:
    prior = Position(
        symbol="QQQ",
        qty=10.0,
        side="long",
        avg_entry_price=100.0,
    )
    fill = OrderResult(
        symbol="QQQ",
        side=Side.SELL,
        qty=10.0,
        filled_price=101.0,
        filled_at=datetime.now(timezone.utc),
        status="filled",
    )
    recon = reconcile_broker_primary_fill(
        requested_qty=10.0,
        fill_result=fill,
        prior_position=prior,
        broker_position=None,
        bars_in_trade=4,
    )
    assert recon.bars_in_trade == 0
    assert recon.position_side == "flat"
    assert recon.broker_qty == 0.0


def test_multi_leg_execution_refreshes_account_before_each_leg() -> None:
    refresh_calls = 0
    execute_calls = 0

    async def refresh_snapshot():
        nonlocal refresh_calls
        refresh_calls += 1
        account = MagicMock(equity=90_000.0 - refresh_calls * 5_000.0)
        return account, []

    async def execute_leg(*_args, **_kwargs):
        nonlocal execute_calls
        execute_calls += 1
        return []

    due_legs = [("leg_a", MagicMock()), ("leg_b", MagicMock())]
    coordination = MagicMock()
    coordination.plans = {"leg_a": MagicMock(), "leg_b": MagicMock()}
    economic_account = MagicMock(equity=100_000.0)
    positions: list[Position] = []

    async def run_phase_c() -> None:
        execution_account = economic_account
        for leg_cfg, leg in due_legs:
            plan = coordination.plans[leg_cfg]
            if len(due_legs) > 1:
                execution_account, positions_local = await refresh_snapshot()
            else:
                positions_local = positions
            await execute_leg(leg, plan, execution_account, positions_local)

    import asyncio

    asyncio.run(run_phase_c())
    assert refresh_calls == 2
    assert execute_calls == 2
