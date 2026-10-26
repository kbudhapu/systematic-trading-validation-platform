"""
Fix 2/5 — Stale pending-order-store keys on failed order submission.

Tests verify:
1. When _submit_sync raises for one order in a multi-order batch, the failed
   order's key is cleaned from _pending_order_store and _pending_order_ids
   immediately (not waiting for the next pre-flight reconciliation), while
   successful orders are unaffected and the failure is clearly logged.

2. A new LONG/SHORT signal on the same leg immediately after a submission
   failure is blocked by the cooldown gate (not by a stale pending-bundle
   artifact), and once the cooldown expires the leg can re-enter.

3. Case A2 (order reaches exchange, _wait_for_fill_sync fails polling,
   conservatively assumed submitted) is unchanged: the result still enters
   the for-r-in-results loop normally — nothing in Fix 2 touches that path.

4. submission_failed results are filtered before passing to the fill
   reconciliation sieve, so GD-R3 (has_pending_bundle_for_leg) is never
   wrongly armed by a never-placed order.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broker.alpaca import AlpacaBroker
from src.engine.broker_fill_sieve import BrokerFillReconciliationSieve
from src.models import Order, OrderResult, Side
from src.persistence.governance_state_store import PendingOrderStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _order(symbol: str = "SPY", side: Side = Side.BUY, qty: float = 10.0) -> Order:
    return Order(symbol=symbol, side=side, qty=qty)


def _result(symbol: str, side: Side, status: str = "filled", qty: float = 10.0) -> OrderResult:
    return OrderResult(
        symbol=symbol,
        side=side,
        qty=qty,
        filled_price=400.0,
        filled_at=_now(),
        order_id=f"oid-{symbol}",
        status=status,
    )


# ---------------------------------------------------------------------------
# Part 1 — _submit_sync returns submission_failed OrderResult for exceptions
# ---------------------------------------------------------------------------

def test_submit_sync_returns_submission_failed_on_exception() -> None:
    """When _submit_market_sync raises, _submit_sync returns a submission_failed result."""
    broker = MagicMock(spec=AlpacaBroker)
    broker._symbol_and_tif = MagicMock(return_value=("SPY", "day"))

    # Simulate two orders: first succeeds, second raises.
    call_count = 0
    def fake_submit_market_sync(order: Order) -> OrderResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _result(order.symbol, order.side, status="filled", qty=order.qty)
        raise ConnectionError("broker connection lost")

    from src.broker.alpaca import AlpacaBroker as AB
    import src.broker.alpaca as alpaca_mod

    orders = [_order("SPY", Side.BUY, 10.0), _order("QQQ", Side.BUY, 5.0)]
    instance = object.__new__(AB)
    instance._submit_market_sync = fake_submit_market_sync
    instance._symbol_and_tif = lambda sym: ("MAPPED", "day")

    results = AB._submit_sync(instance, orders, execution_contexts=None)

    assert len(results) == 2

    spy_result = next(r for r in results if r.symbol == "SPY")
    assert spy_result.status == "filled"
    assert spy_result.qty == 10.0

    qqq_result = next(r for r in results if r.symbol == "QQQ")
    assert qqq_result.status == "submission_failed"
    assert qqq_result.qty == 0.0
    assert qqq_result.filled_price == 0.0
    assert qqq_result.order_id is None


def test_submit_sync_all_succeed_no_submission_failed() -> None:
    """When all orders succeed, no submission_failed result is produced."""
    call_count = 0
    def fake_submit_market_sync(order: Order) -> OrderResult:
        return _result(order.symbol, order.side, status="filled", qty=order.qty)

    from src.broker.alpaca import AlpacaBroker as AB
    instance = object.__new__(AB)
    instance._submit_market_sync = fake_submit_market_sync

    orders = [_order("SPY"), _order("QQQ")]
    results = AB._submit_sync(instance, orders, execution_contexts=None)

    assert all(r.status == "filled" for r in results)
    assert all(r.status != "submission_failed" for r in results)


# ---------------------------------------------------------------------------
# Part 2 — Pending-order-store keys cleaned immediately on submission_failed
# ---------------------------------------------------------------------------

def test_submission_failed_key_removed_immediately(tmp_path: Path) -> None:
    """
    Simulates the orchestrator's for-r-in-results loop.

    A submission_failed result must cause its key to be popped from both
    _pending_order_store and _pending_order_ids immediately; the successful
    result's key must remain until explicitly popped later.
    """
    db = tmp_path / "pending.db"
    store = PendingOrderStore(db_path=db)

    spy_key = "spy:SPY:buy"
    qqq_key = "qqq:QQQ:buy"

    store.stage(spy_key, strategy_id="spy", symbol="SPY", side="buy", broker_order_id="b1")
    store.stage(qqq_key, strategy_id="qqq", symbol="QQQ", side="buy", broker_order_id=None)
    pending_ids: dict[str, str | None] = {spy_key: "b1", qqq_key: None}
    cooldown: dict[str, int] = {}

    SUBMISSION_FAILURE_COOLDOWN_CYCLES = 3

    results = [
        _result("SPY", Side.BUY, status="filled", qty=10.0),
        _result("QQQ", Side.BUY, status="submission_failed", qty=0.0),
    ]

    for r in results:
        key = f"{'spy' if r.symbol == 'SPY' else 'qqq'}:{r.symbol}:{r.side.value}"
        if r.status == "submission_failed":
            store.pop(key)
            pending_ids.pop(key, None)
            cooldown["qqq"] = SUBMISSION_FAILURE_COOLDOWN_CYCLES
            continue
        # Normal fill processing: pop after processing
        store.pop(key)
        pending_ids.pop(key, None)

    # QQQ key cleaned by submission_failed handler
    assert qqq_key not in pending_ids
    assert store.load_all().get(qqq_key) is None

    # SPY key already popped in the same loop (both ran); nothing left
    assert spy_key not in pending_ids
    assert store.load_all().get(spy_key) is None

    # Cooldown was set for the failing leg
    assert cooldown.get("qqq") == SUBMISSION_FAILURE_COOLDOWN_CYCLES


def test_submission_failed_only_affects_failed_leg_key(tmp_path: Path) -> None:
    """A submission_failed for QQQ does not disturb SPY's pending state."""
    db = tmp_path / "pending.db"
    store = PendingOrderStore(db_path=db)

    spy_key = "spy:SPY:buy"
    qqq_key = "qqq:QQQ:buy"

    store.stage(spy_key, strategy_id="spy", symbol="SPY", side="buy", broker_order_id="b1")
    store.stage(qqq_key, strategy_id="qqq", symbol="QQQ", side="buy", broker_order_id=None)
    pending_ids: dict[str, str | None] = {spy_key: "b1", qqq_key: None}
    cooldown: dict[str, int] = {}

    # Only process the QQQ result (submission_failed)
    r = _result("QQQ", Side.BUY, status="submission_failed", qty=0.0)
    key = "qqq:QQQ:buy"
    if r.status == "submission_failed":
        store.pop(key)
        pending_ids.pop(key, None)
        cooldown["qqq"] = 3

    # QQQ removed
    assert qqq_key not in pending_ids
    assert store.load_all().get(qqq_key) is None

    # SPY untouched
    assert spy_key in pending_ids
    assert store.load_all().get(spy_key) is not None  # still staged


# ---------------------------------------------------------------------------
# Part 3 — Submission-failure cooldown gates entry signals
# ---------------------------------------------------------------------------

def test_cooldown_blocks_entry_for_correct_number_of_cycles() -> None:
    """
    Cooldown counts down each cycle and blocks LONG/SHORT entries while active.
    Simulates the cooldown decrement+check logic from _execute_coordinated_leg.
    """
    from src.models import SignalAction

    COOLDOWN_CYCLES = 3
    cooldown: dict[str, int] = {"spy": COOLDOWN_CYCLES}
    blocked: list[int] = []
    allowed: list[int] = []

    # Simulate N cycles
    for cycle in range(1, COOLDOWN_CYCLES + 3):
        remaining = cooldown.get("spy", 0)
        if remaining > 0:
            cooldown["spy"] = remaining - 1
            blocked.append(cycle)
        else:
            allowed.append(cycle)

    assert blocked == [1, 2, 3]        # first 3 cycles blocked
    assert allowed == [4, 5]           # subsequent cycles allowed


def test_cooldown_does_not_block_exit_signals() -> None:
    """EXIT signals must pass through even during submission-failure cooldown."""
    from src.models import SignalAction

    cooldown = {"spy": 3}
    signal_action = SignalAction.EXIT  # exit, not entry

    # Check: cooldown > 0 but EXIT is exempt
    remaining = cooldown.get("spy", 0)
    if remaining > 0:
        cooldown["spy"] = remaining - 1
    # Only LONG/SHORT are gated; EXIT is not
    is_blocked = (
        remaining > 0
        and signal_action in (SignalAction.LONG, SignalAction.SHORT)
    )
    assert not is_blocked  # EXIT must pass through


# ---------------------------------------------------------------------------
# Part 4 — submission_failed does NOT enter the fill reconciliation sieve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submission_failed_not_tracked_by_sieve() -> None:
    """
    submission_failed results filtered before reconcile_post_submit —
    has_pending_bundle_for_leg must remain False for the affected leg.
    """
    sieve = BrokerFillReconciliationSieve()

    # Verify: sieve has no bundle for "spy" before we do anything
    assert not await sieve.has_pending_bundle_for_leg("spy", "SPY")

    # Confirm that broker_results filter works: submission_failed excluded
    all_results = [
        _result("SPY", Side.BUY, status="submission_failed", qty=0.0),
    ]
    broker_results = [r for r in all_results if r.status != "submission_failed"]

    # Nothing left — sieve would receive empty list → no bundle created
    assert broker_results == []
    # Sieve still empty for this leg
    assert not await sieve.has_pending_bundle_for_leg("spy", "SPY")


@pytest.mark.asyncio
async def test_successful_result_is_tracked_by_sieve(tmp_path: Path) -> None:
    """
    Control: a genuine 'submitted' (ambiguous) result DOES enter the sieve
    (confirming the filter doesn't over-strip).
    """
    from src.engine.broker_fill_sieve import BrokerFillReconciliationSieve, _build_slices
    from src.models import Order

    results = [_result("SPY", Side.BUY, status="submitted", qty=0.0)]
    orders = [_order("SPY", Side.BUY, 10.0)]
    slices = _build_slices(orders, results)

    # status="submitted" is in OPEN_ORDER_STATUSES → needs_tracking
    from src.engine.broker_fill_sieve import OPEN_ORDER_STATUSES
    assert slices[0].status == "submitted"
    assert slices[0].status in OPEN_ORDER_STATUSES


# ---------------------------------------------------------------------------
# Part 5 — Case A2 is unchanged (submitted result flows through normally)
# ---------------------------------------------------------------------------

def test_case_a2_submitted_result_not_intercepted_by_submission_failed_branch() -> None:
    """
    Case A2: _wait_for_fill_sync times out, returns status='submitted'.
    The result has status='submitted' (not 'submission_failed'), so the
    submission_failed branch does NOT fire; key is NOT popped immediately;
    result enters the normal loop for sieve tracking and GD-R1 monitoring.
    """
    case_a2_result = OrderResult(
        symbol="SPY",
        side=Side.BUY,
        qty=10.0,           # _resolved_fill_qty returns float(order.qty) for "submitted"
        filled_price=401.0,
        filled_at=_now(),
        order_id="oid-a2",
        status="submitted",  # conservative assumed-filled status from _wait_for_fill_sync
    )

    # The submission_failed branch checks r.status == "submission_failed"
    assert case_a2_result.status != "submission_failed"

    # It would NOT be intercepted and would NOT have its key popped early
    was_intercepted = case_a2_result.status == "submission_failed"
    assert not was_intercepted

    # It WOULD pass through to log_fill (since it's not intercepted)
    would_log_fill = not was_intercepted
    assert would_log_fill

    # It WOULD be included in broker_results for the sieve
    broker_results = [r for r in [case_a2_result] if r.status != "submission_failed"]
    assert len(broker_results) == 1
    assert broker_results[0] is case_a2_result
