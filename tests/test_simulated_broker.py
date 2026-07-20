"""Tests for simulated broker cash accounting."""

from __future__ import annotations

from src.broker.simulated import SimulatedBroker
from src.models import Order, Side


# ── existing long-side tests ────────────────────────────────────────────────


def test_simulated_broker_caps_long_by_cash():
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    order = Order(symbol="SPY", side=Side.BUY, qty=10_000, strategy_id="test")
    results = broker.submit_orders([order], fill_price=400.0)
    assert len(results) == 1
    assert results[0].qty == 250
    assert broker.cash == 0.0
    assert broker.equity == 100_000.0


def test_simulated_broker_close_long_credits_proceeds():
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="SPY", side=Side.BUY, qty=100, strategy_id="t")],
        fill_price=100.0,
    )
    broker.submit_orders(
        [Order(symbol="SPY", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=110.0,
    )
    assert broker.cash == 101_000.0
    assert broker.get_positions() == []


def test_close_all_positions():
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="SPY", side=Side.BUY, qty=100, strategy_id="t")],
        fill_price=100.0,
    )
    broker.close_all_positions(mark_price=105.0)
    assert broker.get_positions() == []
    assert broker.cash == 100_500.0


# ── short-side tests ────────────────────────────────────────────────────────


def test_open_short_credits_cash():
    """Opening a short credits proceeds to cash and records a short position."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    results = broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    assert len(results) == 1
    assert results[0].qty == 100
    assert broker.cash == 120_000.0  # 100k + 100*200 proceeds
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].side == "short"
    assert positions[0].qty == 100
    assert positions[0].avg_entry_price == 200.0


def test_close_short_profit():
    """Covering at a lower price realises a profit."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.BUY, qty=100, strategy_id="t")],
        fill_price=180.0,
    )
    # cash: 100k + 20k proceeds − 18k cover = 102k
    assert broker.cash == 102_000.0
    assert broker.get_positions() == []
    assert broker.equity == 102_000.0


def test_close_short_loss():
    """Covering at a higher price realises a loss."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.BUY, qty=100, strategy_id="t")],
        fill_price=220.0,
    )
    # cash: 100k + 20k − 22k = 98k
    assert broker.cash == 98_000.0
    assert broker.get_positions() == []
    assert broker.equity == 98_000.0


def test_mark_to_market_while_short():
    """Equity reflects unrealized short P&L: equity = cash − mark × qty."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    # cash = 120k after shorting 100@200
    broker._mark_equity(210.0)  # price moves against us
    # equity = 120k − 210*100 = 99k  (unrealised loss of 1k)
    assert broker.equity == 99_000.0

    broker._mark_equity(190.0)  # price moves in our favour
    # equity = 120k − 190*100 = 101k  (unrealised gain of 1k)
    assert broker.equity == 101_000.0


def test_flip_long_to_short():
    """Risk-manager flip: two SELL orders — first closes long, second opens short."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.BUY, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    # cash = 80k, long 100@200
    broker.submit_orders(
        [
            Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t"),  # close long
            Order(symbol="GLD", side=Side.SELL, qty=50, strategy_id="t"),   # open short
        ],
        fill_price=210.0,
    )
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].side == "short"
    assert positions[0].qty == 50
    # cash: 80k + 100*210 (close long) + 50*210 (short proceeds) = 80k+21k+10.5k = 111.5k
    assert broker.cash == 111_500.0


def test_short_caps_by_margin():
    """Short qty is clipped when the 50% margin requirement exceeds available cash."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    # margin_per_share = 200 * 0.50 = 100; max qty = int(100k / 100) = 1000
    results = broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=2_000, strategy_id="t")],
        fill_price=200.0,
    )
    assert results[0].qty == 1_000


def test_close_all_positions_shorts():
    """close_all_positions covers short positions at the mark price."""
    broker = SimulatedBroker(initial_equity=100_000.0, slippage_pct=0.0)
    broker.submit_orders(
        [Order(symbol="GLD", side=Side.SELL, qty=100, strategy_id="t")],
        fill_price=200.0,
    )
    # cash = 120k
    broker.close_all_positions(mark_price=190.0)
    assert broker.get_positions() == []
    # cash: 120k − 190*100 = 101k  (profit of 1k)
    assert broker.cash == 101_000.0
