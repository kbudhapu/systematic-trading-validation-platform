"""E1 execution-path unification: the G1.3 idempotency scenarios exercised THROUGH
the IdempotentExecutionAdapter as the orchestrator installs it (the single order
path). All mock — no live or paper order is submitted anywhere."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.engine.engine_preemption import RiskEscalationEngine
from src.execution.idempotent_execution_adapter import (
    DUPLICATE_NOOP_STATUS, IdempotentExecutionAdapter,
)
from src.models import Order, OrderResult, Position, Side

TS = datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc)


class MockBroker:
    """Broker that dedupes on client_order_id (like Alpaca) + scriptable fills."""

    def __init__(self, positions=None) -> None:
        self._orders: dict[str, OrderResult] = {}
        self._positions = positions or []
        self.submit_calls = 0
        self.reject_symbols: set[str] = set()
        self.partial_symbols: set[str] = set()

    async def submit_orders(self, orders, execution_contexts=None):
        self.submit_calls += 1
        results = []
        for o in orders:
            coid = o.client_order_id
            if coid in self._orders:
                results.append(self._orders[coid])
                continue
            if o.symbol in self.reject_symbols:
                status, qty, price = "rejected", 0.0, 0.0
            elif o.symbol in self.partial_symbols:
                status, qty, price = "partially_filled", o.qty / 2.0, 100.0
            else:
                status, qty, price = "filled", o.qty, 100.0
            res = OrderResult(symbol=o.symbol, side=o.side, qty=qty, filled_price=price,
                              filled_at=TS, order_id=coid, status=status)
            self._orders[coid] = res
            results.append(res)
        return results

    async def get_order_by_client_order_id(self, coid):
        return self._orders.get(coid)

    async def get_positions(self):
        return list(self._positions)

    # a proxied method to prove __getattr__ passthrough
    def some_broker_only_method(self):
        return "inner"


def _order(symbol="QQQ", side=Side.BUY, qty=10.0, leg="mean_reversion_qqq", coid=None):
    return Order(symbol=symbol, side=side, qty=qty, strategy_id=leg, client_order_id=coid)


def _run(coro):
    return asyncio.run(coro)


# --- proxy transparency ---------------------------------------------------- #

def test_adapter_proxies_inner_broker_methods() -> None:
    adapter = IdempotentExecutionAdapter(MockBroker())
    assert adapter.some_broker_only_method() == "inner"


# --- deterministic id + normal flow (behavior-preserving) ------------------ #

def test_deterministic_client_order_id_restart_stable() -> None:
    o1, o2 = _order(), _order()
    IdempotentExecutionAdapter.assign_client_order_id(o1, signal_ts=TS)
    IdempotentExecutionAdapter.assign_client_order_id(o2, signal_ts=TS)
    assert o1.client_order_id == o2.client_order_id and o1.client_order_id.startswith("mbappe-")
    # a different side -> different id
    o3 = _order(side=Side.SELL)
    IdempotentExecutionAdapter.assign_client_order_id(o3, signal_ts=TS)
    assert o3.client_order_id != o1.client_order_id


def test_normal_submit_delegates_and_fills() -> None:
    broker = MockBroker()
    adapter = IdempotentExecutionAdapter(broker)
    o = _order()
    IdempotentExecutionAdapter.assign_client_order_id(o, signal_ts=TS)
    res = _run(adapter.submit_orders([o]))
    assert len(res) == 1 and res[0].status == "filled" and res[0].qty == 10.0
    assert broker.submit_calls == 1


# --- duplicate-submit storm THROUGH the adapter ---------------------------- #

def test_dup_submit_storm_through_adapter_one_order() -> None:
    broker = MockBroker()
    adapter = IdempotentExecutionAdapter(broker)

    async def storm():
        coid = "mbappe-fixedid"
        calls = [adapter.submit_orders([_order(coid=coid)]) for _ in range(10)]
        return await asyncio.gather(*calls)

    results = _run(storm())
    flat = [r for batch in results for r in batch]
    filled = [r for r in flat if r.status == "filled"]
    noops = [r for r in flat if r.status == DUPLICATE_NOOP_STATUS]
    assert len(broker._orders) == 1, "exactly one order reaches the broker"
    assert len(filled) == 1 and len(noops) == 9


def test_retry_same_id_is_noop() -> None:
    broker = MockBroker()
    adapter = IdempotentExecutionAdapter(broker)
    o = _order(coid="mbappe-retry")
    first = _run(adapter.submit_orders([o]))
    second = _run(adapter.submit_orders([_order(coid="mbappe-retry")]))
    assert first[0].status == "filled" and second[0].status == DUPLICATE_NOOP_STATUS


# --- partial fill / rejection pass through --------------------------------- #

def test_partial_fill_passes_through() -> None:
    broker = MockBroker()
    broker.partial_symbols.add("QQQ")
    adapter = IdempotentExecutionAdapter(broker)
    o = _order(coid="mbappe-partial")
    res = _run(adapter.submit_orders([o]))
    assert res[0].status == "partially_filled" and res[0].qty == 5.0


def test_rejection_passes_through() -> None:
    broker = MockBroker()
    broker.reject_symbols.add("QQQ")
    adapter = IdempotentExecutionAdapter(broker)
    res = _run(adapter.submit_orders([_order(coid="mbappe-reject")]))
    assert res[0].status == "rejected"


# --- preemption mid-order leaves no orphan; restart dedupes ---------------- #

def test_preemption_then_reboot_resubmit_dedupes() -> None:
    """Sim: an order is reserved+submitted (its id now at the broker). A reboot
    reconstructs the SAME deterministic id (same signal ts) and re-submits -> the
    adapter dedupes, so there is exactly one order (no orphan/duplicate)."""
    broker = MockBroker()
    adapter1 = IdempotentExecutionAdapter(broker)
    o = _order()
    IdempotentExecutionAdapter.assign_client_order_id(o, signal_ts=TS)
    _run(adapter1.submit_orders([o]))
    # fresh adapter (process restart), same broker, same deterministic id:
    adapter2 = IdempotentExecutionAdapter(broker)
    o2 = _order()
    IdempotentExecutionAdapter.assign_client_order_id(o2, signal_ts=TS)
    assert o2.client_order_id == o.client_order_id
    res = _run(adapter2.submit_orders([o2]))
    assert res[0].status == DUPLICATE_NOOP_STATUS and len(broker._orders) == 1


# --- boot reconciliation --------------------------------------------------- #

def test_reconcile_boot_clean_no_trip() -> None:
    broker = MockBroker(positions=[Position("QQQ", 10.0, "long", 400.0)])
    esc = RiskEscalationEngine()
    adapter = IdempotentExecutionAdapter(broker, escalation=esc)
    report = _run(adapter.reconcile_boot({"QQQ": 10.0}))
    assert report.clean and not esc.blocks_all_entries()


def test_reconcile_boot_mismatch_trips_and_reports() -> None:
    reports: list = []
    broker = MockBroker(positions=[Position("QQQ", 7.0, "long", 400.0)])
    esc = RiskEscalationEngine()
    adapter = IdempotentExecutionAdapter(broker, escalation=esc, report_sink=reports.append)
    report = _run(adapter.reconcile_boot({"QQQ": 10.0}))
    assert report.tripped and esc.blocks_all_entries()
    assert reports and reports[0]["kind"] == "boot_reconciliation_mismatch"
