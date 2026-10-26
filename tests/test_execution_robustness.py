"""G1.3 execution robustness -- ALL order-path testing uses a MOCK broker; no
live or paper order is ever submitted. Covers idempotent submission (dup-storm),
partial fills, cancels, rejections, boot reconciliation (clean + mismatched), and
the EnginePreemptedException lifecycle (no orphan + restart idempotency)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from src.engine.engine_preemption import (
    EnginePreemptedException, RiskEscalationEngine, RiskEscalationLevel,
)
from src.execution.idempotent_execution import (
    BrokerOrder, IdempotentSubmitter, OrderIntent, PositionTracker,
    deterministic_client_order_id, reconcile_boot,
)

_TS = datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc)


class MockExecutionBroker:
    """In-memory broker. Dedupes on client_order_id; supports scripted
    fills/cancels/rejections. Thread-safe for the dup-storm test."""

    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, float] = {}
        self._lock = threading.Lock()
        self.reject_symbols: set[str] = set()
        self.submit_calls = 0

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        with self._lock:
            self.submit_calls += 1
            coid = intent.client_order_id
            existing = self._orders.get(coid)
            if existing is not None:
                return existing
            status = "rejected" if intent.symbol in self.reject_symbols else "new"
            order = BrokerOrder(
                client_order_id=coid, symbol=intent.symbol, side=intent.side,
                qty=intent.qty, status=status, order_id=f"ord-{len(self._orders) + 1}")
            self._orders[coid] = order
            return order

    def get_by_client_order_id(self, coid: str) -> BrokerOrder | None:
        with self._lock:
            return self._orders.get(coid)

    def open_orders(self) -> list[BrokerOrder]:
        with self._lock:
            return [o for o in self._orders.values() if not o.is_terminal]

    def positions(self) -> dict[str, float]:
        with self._lock:
            return dict(self._positions)

    # ---- test scripting helpers ----
    def fill(self, coid: str, qty: float, price: float, *, terminal: bool = True) -> BrokerOrder:
        o = self._orders[coid]
        o.filled_qty += qty
        o.filled_avg_price = price
        o.status = "filled" if terminal else "partially_filled"
        signed = qty if o.side == "buy" else -qty
        self._positions[o.symbol] = self._positions.get(o.symbol, 0.0) + signed
        return o

    def cancel(self, coid: str) -> BrokerOrder:
        o = self._orders[coid]
        o.status = "canceled"
        return o

    @property
    def distinct_orders(self) -> int:
        return len(self._orders)


def _intent(symbol="QQQ", side="buy", qty=10.0, leg="mean_reversion_qqq", epoch=1) -> OrderIntent:
    return OrderIntent(leg_id=leg, symbol=symbol, side=side, qty=qty, signal_ts=_TS, epoch=epoch)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def test_client_order_id_is_deterministic_and_side_sensitive() -> None:
    a = deterministic_client_order_id("qqq", _TS, "buy", 1)
    b = deterministic_client_order_id("qqq", _TS, "buy", 1)
    assert a == b and a.startswith("mbappe-") and len(a) <= 48
    assert deterministic_client_order_id("qqq", _TS, "sell", 1) != a
    assert deterministic_client_order_id("qqq", _TS, "buy", 2) != a


def test_duplicate_submit_storm_creates_exactly_one_order() -> None:
    """10 concurrent retries of the SAME intent -> exactly 1 broker order; the
    other 9 observe it and NO-OP."""
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    intent = _intent()
    results: list = []
    lock = threading.Lock()

    def worker() -> None:
        r = submitter.submit(intent)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert broker.distinct_orders == 1, "dup-storm must create exactly one order"
    assert sum(1 for r in results if r.was_noop) == 9, "9 of 10 submits must NO-OP"
    assert sum(1 for r in results if not r.was_noop) == 1


def test_retry_after_completion_is_noop() -> None:
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    intent = _intent()
    first = submitter.submit(intent)
    broker.fill(intent.client_order_id, 10.0, 400.0)
    second = submitter.submit(intent)      # retry same id
    assert not first.was_noop and second.was_noop
    assert broker.distinct_orders == 1


# --------------------------------------------------------------------------- #
# Partial fills, cancels, rejections
# --------------------------------------------------------------------------- #

def test_partial_fill_then_completion() -> None:
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    tracker = PositionTracker()
    intent = _intent(qty=10.0)
    submitter.submit(intent)
    coid = intent.client_order_id

    o = broker.fill(coid, 4.0, 400.0, terminal=False)      # partial
    remainder = tracker.apply(BrokerOrder(coid, "QQQ", "buy", 10.0, "partially_filled", 4.0, 400.0))
    assert tracker.position("QQQ") == 4.0 and remainder == 6.0

    o = broker.fill(coid, 6.0, 401.0, terminal=True)       # completes
    tracker.apply(BrokerOrder(coid + "-fill2", "QQQ", "buy", 6.0, "filled", 6.0, 401.0))
    assert tracker.position("QQQ") == 10.0


def test_partial_fill_then_cancel_keeps_filled_qty() -> None:
    broker = MockExecutionBroker()
    tracker = PositionTracker()
    intent = _intent(qty=10.0)
    IdempotentSubmitter(broker).submit(intent)
    coid = intent.client_order_id
    broker.fill(coid, 3.0, 400.0, terminal=False)
    tracker.apply(BrokerOrder(coid, "QQQ", "buy", 10.0, "partially_filled", 3.0, 400.0))
    broker.cancel(coid)
    # cancel leaves the 3 filled shares in the position; remainder is abandoned
    assert tracker.position("QQQ") == 3.0
    assert broker.get_by_client_order_id(coid).status == "canceled"


def test_rejection_leaves_position_unchanged_and_reports() -> None:
    reports: list[dict] = []
    broker = MockExecutionBroker()
    broker.reject_symbols.add("QQQ")
    submitter = IdempotentSubmitter(broker, report_sink=reports.append)
    tracker = PositionTracker()
    intent = _intent()
    res = submitter.submit(intent)
    assert res.order.status == "rejected"
    tracker.apply(res.order)
    assert tracker.position("QQQ") == 0.0, "rejection must not move the position"
    assert any(r["kind"] == "order_rejected" for r in reports), "rejection must emit a report"


def test_position_apply_is_idempotent_under_replay() -> None:
    tracker = PositionTracker()
    o = BrokerOrder("c1", "QQQ", "buy", 5.0, "filled", 5.0, 400.0)
    tracker.apply(o)
    tracker.apply(o)                          # replay (reconciliation / retry)
    assert tracker.position("QQQ") == 5.0, "same fill must not double-count"


# --------------------------------------------------------------------------- #
# Boot reconciliation (no silent adoption)
# --------------------------------------------------------------------------- #

def test_boot_reconcile_clean_no_trip() -> None:
    esc = RiskEscalationEngine()
    report = reconcile_boot({"QQQ": 10.0, "SPY": -5.0}, {"QQQ": 10.0, "SPY": -5.0},
                            escalation=esc)
    assert report.clean and not report.tripped
    assert not esc.blocks_all_entries()


@pytest.mark.parametrize("internal,broker,kind", [
    ({"QQQ": 10.0}, {"QQQ": 7.0}, "position_qty"),
    ({}, {"QQQ": 5.0}, "unknown_broker_position"),
    ({"QQQ": 5.0}, {}, "missing_broker_position"),
])
def test_boot_reconcile_mismatch_trips_block_new_entries(internal, broker, kind) -> None:
    reports: list[dict] = []
    esc = RiskEscalationEngine()
    report = reconcile_boot(internal, broker, escalation=esc, report_sink=reports.append)
    assert not report.clean and report.tripped
    assert report.mismatches[0].kind == kind
    assert esc.blocks_all_entries(), "ANY mismatch must block new entries (SAFE_MODE-equiv)"
    assert reports and reports[0]["kind"] == "boot_reconciliation_mismatch"
    # silent adoption prohibited: reconcile never returns broker state as truth
    assert not hasattr(report, "adopted")


def test_boot_reconcile_orphan_open_order_trips() -> None:
    esc = RiskEscalationEngine()
    report = reconcile_boot({"QQQ": 10.0}, {"QQQ": 10.0},
                            internal_open_coids=set(), broker_open_coids={"mbappe-orphan"},
                            escalation=esc)
    assert report.tripped and report.mismatches[0].kind == "orphan_open_order"
    assert esc.blocks_all_entries()


# --------------------------------------------------------------------------- #
# EnginePreemptedException lifecycle
# --------------------------------------------------------------------------- #

def test_preemption_before_send_leaves_no_orphan() -> None:
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    intent = _intent()
    with pytest.raises(EnginePreemptedException):
        submitter.submit(intent, preempt_check=lambda: True)
    # preempted before the network send -> no order exists at the broker
    assert broker.get_by_client_order_id(intent.client_order_id) is None
    assert broker.distinct_orders == 0


def test_restart_after_preemption_is_idempotent() -> None:
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    intent = _intent()
    with pytest.raises(EnginePreemptedException):
        submitter.submit(intent, preempt_check=lambda: True)
    # restart: preemption cleared -> submit once, then a retry NO-OPs (no duplicate)
    r1 = submitter.submit(intent, preempt_check=lambda: False)
    r2 = submitter.submit(intent, preempt_check=lambda: False)
    assert not r1.was_noop and r2.was_noop
    assert broker.distinct_orders == 1


def test_kill_mid_flight_clean_reboot_reconciles() -> None:
    """Order sent + filled at broker AND applied internally before the kill ->
    reboot reconciliation is clean."""
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    tracker = PositionTracker()
    intent = _intent(qty=10.0)
    submitter.submit(intent)
    o = broker.fill(intent.client_order_id, 10.0, 400.0)
    tracker.apply(o)
    esc = RiskEscalationEngine()
    report = reconcile_boot(tracker.snapshot(), broker.positions(), escalation=esc)
    assert report.clean and not esc.blocks_all_entries()


def test_kill_mid_flight_mismatched_reboot_trips() -> None:
    """Order filled at the broker but the process died BEFORE the internal apply ->
    reboot reconciliation finds the divergence and trips block-new-entries."""
    broker = MockExecutionBroker()
    submitter = IdempotentSubmitter(broker)
    tracker = PositionTracker()          # never updated (killed before apply)
    intent = _intent(qty=10.0)
    submitter.submit(intent)
    broker.fill(intent.client_order_id, 10.0, 400.0)
    esc = RiskEscalationEngine()
    report = reconcile_boot(tracker.snapshot(), broker.positions(), escalation=esc)
    assert report.tripped and esc.blocks_all_entries()
    assert report.mismatches[0].kind == "unknown_broker_position"
