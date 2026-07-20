"""G3.2 kill-switch MOCK drill (for the runbook).

Engages the kill switch (GLOBAL_FLATTEN_AND_HALT) and drives the real
UnifiedFlattenProtocol portfolio-flatten cascade against a MOCK broker -- no live
or paper order is submitted. Proves the kill -> flatten path end to end; the
transcript is pasted into docs/RUNBOOK.md."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.engine.engine_preemption import RiskEscalationEngine, RiskEscalationLevel
from src.engine.flatten_protocol import UnifiedFlattenProtocol
from src.persistence.governance_state_store import PendingOrderStore


@dataclass
class _Pos:
    symbol: str
    qty: float
    side: str
    avg_entry_price: float = 0.0


class MockFlattenBroker:
    """Records the flatten cascade; submits nothing anywhere."""

    def __init__(self, positions: list[_Pos]) -> None:
        self._positions = positions
        self.cancelled: list[str] = []
        self.flattened: list[str] = []
        self.close_all_calls = 0

    async def get_positions(self):
        return list(self._positions)

    async def get_open_orders(self):
        return []

    async def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        self.cancelled.append(symbol)
        return 0

    async def force_cancel_all_orders_for_symbol(self, symbol: str) -> int:
        return 0

    async def await_open_orders_cleared(self, symbol: str, *, timeout_seconds: float):
        return True, 0

    async def force_flatten_symbol_position(self, symbol, *, position_side, position_qty):
        self.flattened.append(symbol)
        return {"symbol": symbol, "status": "filled", "qty": position_qty}

    async def close_all_positions(self) -> None:
        self.close_all_calls += 1


async def test_kill_switch_flatten_drill(tmp_path: Path) -> None:
    print("\n=== KILL-SWITCH MOCK DRILL ===")
    esc = RiskEscalationEngine()
    esc.transition(RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT, commanded_by="operator")
    print(f"[1] kill engaged: level={esc.snapshot().level.value} "
          f"should_flatten_portfolio={esc.should_flatten_portfolio()} "
          f"blocks_all_entries={esc.blocks_all_entries()}")
    assert esc.should_flatten_portfolio() and esc.blocks_all_entries()

    broker = MockFlattenBroker([_Pos("QQQ", 10.0, "long", 400.0), _Pos("SPY", 5.0, "short", 500.0)])
    protocol = UnifiedFlattenProtocol(broker, PendingOrderStore(db_path=tmp_path / "trading.db"))
    result = await protocol.execute_portfolio_flatten(reason="operator_kill_switch")

    print(f"[2] flatten cascade: cancelled={broker.cancelled} flattened={broker.flattened}")
    print(f"[3] result: executed={result.executed} skipped={result.skipped} "
          f"symbols_flattened={result.symbols_flattened} symbols_failed={result.symbols_failed}")
    assert result.executed and not result.skipped
    assert set(result.symbols_flattened) == {"QQQ", "SPY"}
    assert not result.partial_failure
    print("[4] DRILL PASSED: kill -> portfolio flatten of QQQ + SPY, no partial failure, "
          "entries blocked. No live/paper order submitted (mock broker).")


async def test_kill_release_clears_flatten_command() -> None:
    esc = RiskEscalationEngine()
    esc.transition(RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT, commanded_by="operator")
    assert esc.should_flatten_portfolio()
    # engine-shutdown latch stays set until an explicit release (kill outranks all)
    assert esc.is_engine_shutdown_latched()


# --- E1: the single submission gate blocks a live send when the authority says block --------

from src.engine.entry_authority import EntryContext, entry_allowed
from src.execution.idempotent_execution import (
    BrokerOrder,
    EntryBlockedException,
    IdempotentSubmitter,
    OrderIntent,
)


class _RecordingBroker:
    """Minimal ExecutionBroker: records whether an order was actually sent."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        self.submitted.append(intent.client_order_id)
        return BrokerOrder(client_order_id=intent.client_order_id, symbol=intent.symbol,
                           side=intent.side, qty=intent.qty, status="new")

    def get_by_client_order_id(self, coid: str) -> BrokerOrder | None:
        return None

    def open_orders(self):
        return []

    def positions(self):
        return {}


def _intent():
    return OrderIntent(leg_id="mean_reversion_qqq", symbol="QQQ", side="buy", qty=1.0,
                       signal_ts="2026-07-15T14:30:00", epoch=1)


def test_entry_guard_blocks_submission_when_killed():
    """Kill engaged → the composed authority blocks → NO order is sent (EntryBlockedException)."""
    broker = _RecordingBroker()
    submitter = IdempotentSubmitter(broker)
    guard = lambda: entry_allowed(EntryContext(action="LONG", preemption_flatten_portfolio=True))

    raised = False
    try:
        submitter.submit(_intent(), entry_guard=guard)
    except EntryBlockedException as exc:
        raised = True
        assert exc.decision.block_reason == "preemption_escalation"
    assert raised, "submit should have raised EntryBlockedException"
    assert broker.submitted == [], "no order may be sent when the authority blocks"


def test_entry_guard_allows_submission_when_clear():
    broker = _RecordingBroker()
    submitter = IdempotentSubmitter(broker)
    guard = lambda: entry_allowed(EntryContext(action="LONG"))
    result = submitter.submit(_intent(), entry_guard=guard)
    assert result.was_noop is False
    assert broker.submitted == [_intent().client_order_id]


def test_each_store_blocks_submission():
    """Every one of the five stores, engaged alone, blocks the submission gate."""
    for fields in (
        dict(preemption_flatten_portfolio=True),
        dict(portfolio_halted=True),
        dict(pre_flight_recon_locked=True),
        dict(risk_halted=True),
        dict(strategy_halted=True),
    ):
        broker = _RecordingBroker()
        submitter = IdempotentSubmitter(broker)
        guard = lambda f=fields: entry_allowed(EntryContext(action="LONG", **f))
        try:
            submitter.submit(_intent(), entry_guard=guard)
            blocked = False
        except EntryBlockedException:
            blocked = True
        assert blocked and broker.submitted == [], f"store {fields} failed to block submission"
