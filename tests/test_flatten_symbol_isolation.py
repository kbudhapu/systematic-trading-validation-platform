"""
Fix 3/5 — Per-symbol exception isolation in execute_portfolio_flatten().

Tests verify:
1. When force_flatten_symbol_position() raises for the FIRST symbol in a
   multi-symbol portfolio, the remaining symbols are still attempted and
   flattened in the SAME call (not deferred to a future cycle).

2. FlattenResult correctly distinguishes full success (symbols_failed=())
   from partial success (symbols_failed non-empty) — these are not silently
   identical.

3. The existing next-cycle recovery path (should_flatten_portfolio() →
   _execute_portfolio_flatten()) still works for whatever symbols remain
   unflattened — this fix improves single-pass resilience without replacing
   the multi-cycle safety net.

4. No regression in the already-correct single-symbol success path.

5. execute_leg_flatten() is single-symbol throughout — no per-symbol loop
   exists there, so the confirmed finding does not apply (verified).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.engine.flatten_protocol import FlattenResult, UnifiedFlattenProtocol
from src.persistence.governance_state_store import PendingOrderStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _position(symbol: str, qty: float = 10.0, side: str = "long") -> MagicMock:
    m = MagicMock()
    m.symbol = symbol
    m.qty = qty
    m.side = side
    return m


def _make_protocol(tmp_path: Path, broker: MagicMock) -> UnifiedFlattenProtocol:
    store = PendingOrderStore(db_path=tmp_path / "trading.db")
    return UnifiedFlattenProtocol(broker, store)


def _broker_with_positions(*positions) -> MagicMock:
    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=list(positions))
    broker.cancel_open_orders_for_symbol = AsyncMock(return_value=0)
    broker.await_open_orders_cleared = AsyncMock(return_value=(True, 0))
    broker.force_cancel_all_orders_for_symbol = AsyncMock(return_value=0)
    broker.close_all_positions = AsyncMock()
    return broker


# ---------------------------------------------------------------------------
# Part 1 — per-symbol exception isolation in the position-flatten loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_symbol_failure_does_not_abort_remaining_symbols(tmp_path: Path) -> None:
    """
    If force_flatten_symbol_position raises for the FIRST symbol (SPY),
    the protocol must still attempt QQQ and BTC in the same call.
    """
    broker = _broker_with_positions(
        _position("SPY"),
        _position("QQQ"),
        _position("BTC/USD"),
    )

    call_order: list[str] = []

    async def fake_flatten(symbol: str, *, position_side: str, position_qty: float):
        call_order.append(symbol)
        if symbol == "SPY":
            raise ConnectionError("broker blip for SPY")
        return MagicMock()

    broker.force_flatten_symbol_position = fake_flatten

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_isolation")

    # All three symbols were attempted
    assert "SPY" in call_order
    assert "QQQ" in call_order
    assert "BTC/USD" in call_order

    # SPY failed, others succeeded
    assert "SPY" in result.symbols_failed
    assert "QQQ" in result.symbols_flattened
    assert "BTC/USD" in result.symbols_flattened

    # The pass still executed
    assert result.executed is True
    assert result.skipped is False


@pytest.mark.asyncio
async def test_middle_symbol_failure_does_not_abort_subsequent_symbols(tmp_path: Path) -> None:
    """If QQQ raises, BTC/USD (which comes after) is still attempted."""
    broker = _broker_with_positions(
        _position("BTC/USD"),
        _position("QQQ"),
        _position("USO"),
    )

    async def fake_flatten(symbol: str, *, position_side: str, position_qty: float):
        if symbol == "QQQ":
            raise RuntimeError("timeout for QQQ")
        return MagicMock()

    broker.force_flatten_symbol_position = fake_flatten

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_middle_failure")

    assert "QQQ" in result.symbols_failed
    assert "BTC/USD" in result.symbols_flattened
    assert "USO" in result.symbols_flattened
    assert result.partial_failure is True


@pytest.mark.asyncio
async def test_all_symbols_fail_returns_all_in_symbols_failed(tmp_path: Path) -> None:
    """If every symbol raises, symbols_failed contains all of them, executed=True."""
    broker = _broker_with_positions(_position("SPY"), _position("QQQ"))

    async def fake_flatten(symbol: str, *, position_side: str, position_qty: float):
        raise ConnectionError("total broker outage")

    broker.force_flatten_symbol_position = fake_flatten

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_all_fail")

    assert set(result.symbols_failed) == {"SPY", "QQQ"}
    assert result.symbols_flattened == ()
    assert result.executed is True
    assert result.partial_failure is True


# ---------------------------------------------------------------------------
# Part 2 — FlattenResult distinguishes full success from partial failure
# ---------------------------------------------------------------------------

def test_flatten_result_full_success_has_empty_symbols_failed() -> None:
    r = FlattenResult(
        executed=True,
        skipped=False,
        reason="test",
        symbols_flattened=("SPY", "QQQ"),
        symbols_failed=(),
    )
    assert r.partial_failure is False
    assert r.symbols_failed == ()


def test_flatten_result_partial_failure_flagged_by_property() -> None:
    r = FlattenResult(
        executed=True,
        skipped=False,
        reason="test",
        symbols_flattened=("QQQ",),
        symbols_failed=("SPY",),
        metadata={"position_count": 2, "failed_errors": {"SPY": "timeout"}},
    )
    assert r.partial_failure is True
    assert "SPY" in r.symbols_failed
    assert "QQQ" in r.symbols_flattened
    assert r.metadata["failed_errors"]["SPY"] == "timeout"


def test_flatten_result_skipped_has_no_partial_failure() -> None:
    r = FlattenResult(
        executed=False,
        skipped=True,
        reason="portfolio_flatten_already_active",
    )
    assert r.partial_failure is False
    assert r.symbols_failed == ()
    assert r.symbols_flattened == ()


@pytest.mark.asyncio
async def test_full_success_result_matches_all_positions(tmp_path: Path) -> None:
    """Smoke test: all succeed → symbols_failed=(), symbols_flattened=all."""
    broker = _broker_with_positions(_position("SPY"), _position("QQQ"))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("smoke")

    assert result.executed is True
    assert result.partial_failure is False
    assert set(result.symbols_flattened) == {"SPY", "QQQ"}
    assert result.symbols_failed == ()


# ---------------------------------------------------------------------------
# Part 3 — next-cycle recovery path still works after partial failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_portfolio_flattening_flag_cleared_after_partial_failure(tmp_path: Path) -> None:
    """
    _portfolio_flattening must be False after the call even when a symbol raises,
    so the next-cycle recovery path (which calls execute_portfolio_flatten again)
    is not blocked by the duplicate-guard.
    """
    broker = _broker_with_positions(_position("SPY"), _position("QQQ"))

    async def fake_flatten(symbol: str, *, position_side: str, position_qty: float):
        if symbol == "SPY":
            raise RuntimeError("transient error")
        return MagicMock()

    broker.force_flatten_symbol_position = fake_flatten

    protocol = _make_protocol(tmp_path, broker)
    first_result = await protocol.execute_portfolio_flatten("first_pass")

    assert first_result.partial_failure is True
    # Flag is cleared — next-cycle retry is not blocked
    assert protocol.portfolio_flattening is False

    # Second call (simulating next-cycle recovery) must not return skipped
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())
    second_result = await protocol.execute_portfolio_flatten("recovery_retry")

    assert second_result.executed is True
    assert second_result.skipped is False


@pytest.mark.asyncio
async def test_partial_failure_pending_orders_still_cleared(tmp_path: Path) -> None:
    """
    clear_all() on _pending_orders happens before the position-flatten loop,
    so it runs even when some symbols raise during flatten.
    """
    db = tmp_path / "trading.db"
    store = PendingOrderStore(db_path=db)
    store.stage("spy:SPY:buy", strategy_id="spy", symbol="SPY",
                side="buy", broker_order_id="b1")
    store.stage("qqq:QQQ:buy", strategy_id="qqq", symbol="QQQ",
                side="buy", broker_order_id="b2")

    broker = _broker_with_positions(_position("SPY"), _position("QQQ"))

    async def fake_flatten(symbol: str, *, position_side: str, position_qty: float):
        if symbol == "SPY":
            raise RuntimeError("blip")
        return MagicMock()

    broker.force_flatten_symbol_position = fake_flatten

    protocol = UnifiedFlattenProtocol(broker, store)
    await protocol.execute_portfolio_flatten("test")

    # Pending orders cleared regardless of flatten partial failure
    assert store.load_all() == {}


# ---------------------------------------------------------------------------
# Part 4 — cancel-loop and await-loop per-symbol isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_loop_failure_does_not_abort_subsequent_symbols(tmp_path: Path) -> None:
    """
    If cancel_open_orders_for_symbol raises for SPY, QQQ must still be
    cancelled in the same pass.
    """
    broker = _broker_with_positions(_position("QQQ"), _position("SPY"))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())

    cancel_calls: list[str] = []

    async def fake_cancel(symbol: str) -> int:
        cancel_calls.append(symbol)
        if symbol == "QQQ":
            raise ConnectionError("cancel blip for QQQ")
        return 1

    broker.cancel_open_orders_for_symbol = fake_cancel

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_cancel_isolation")

    # Both symbols were attempted for cancel despite QQQ raising
    assert "QQQ" in cancel_calls
    assert "SPY" in cancel_calls
    # Flatten still proceeded
    assert result.executed is True


@pytest.mark.asyncio
async def test_await_loop_failure_does_not_abort_subsequent_symbols(tmp_path: Path) -> None:
    """
    If await_open_orders_cleared raises for SPY, QQQ must still go through
    the await path in the same pass.
    """
    broker = _broker_with_positions(_position("QQQ"), _position("SPY"))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())

    await_calls: list[str] = []

    async def fake_await(symbol: str, *, timeout_seconds: float):
        await_calls.append(symbol)
        if symbol == "QQQ":
            raise TimeoutError("await timeout for QQQ")
        return (True, 0)

    broker.await_open_orders_cleared = fake_await

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_await_isolation")

    assert "QQQ" in await_calls
    assert "SPY" in await_calls
    assert result.executed is True


# ---------------------------------------------------------------------------
# Part 5 — execute_leg_flatten() is single-symbol — confirmed not affected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leg_flatten_is_single_symbol_no_loop_vulnerability(tmp_path: Path) -> None:
    """
    execute_leg_flatten() operates on a single symbol end-to-end.
    There is no per-symbol loop, so the confirmed finding does not apply.
    This test confirms the function completes correctly for a single symbol.
    """
    broker = MagicMock()
    broker.cancel_open_orders_for_symbol = AsyncMock(return_value=0)
    broker.await_open_orders_cleared = AsyncMock(return_value=(True, 0))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())

    protocol = _make_protocol(tmp_path, broker)
    cleared = await protocol.execute_leg_flatten(
        strategy_id="spy",
        symbol="SPY",
        position_side="long",
        position_qty=10.0,
        reason="strategy_liquidate",
    )

    assert cleared is True
    broker.force_flatten_symbol_position.assert_called_once_with(
        "SPY", position_side="long", position_qty=10.0
    )


# ---------------------------------------------------------------------------
# Part 6 — regression: existing test scenarios still pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_qty_positions_are_skipped(tmp_path: Path) -> None:
    """Positions with qty=0 are skipped; flatten is still executed=True."""
    broker = _broker_with_positions(_position("SPY", qty=0.0), _position("QQQ", qty=5.0))
    broker.force_flatten_symbol_position = AsyncMock(return_value=MagicMock())

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_zero_qty")

    assert result.executed is True
    assert result.symbols_flattened == ("QQQ",)
    assert result.partial_failure is False
    # force_flatten_symbol_position only called for QQQ (non-zero qty)
    broker.force_flatten_symbol_position.assert_called_once()


@pytest.mark.asyncio
async def test_no_positions_calls_close_all(tmp_path: Path) -> None:
    """When broker returns empty positions, close_all_positions is called."""
    broker = _broker_with_positions()

    protocol = _make_protocol(tmp_path, broker)
    result = await protocol.execute_portfolio_flatten("test_empty")

    assert result.executed is True
    assert result.symbols_flattened == ()
    assert result.partial_failure is False
    broker.close_all_positions.assert_called_once()
