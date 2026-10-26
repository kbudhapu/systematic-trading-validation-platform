"""
Simulated broker for backtesting.

Applies slippage (and an optional per-side taker fee) on immediate fills;
synchronous by design (backtest is offline).
Uses cash-proceeds accounting (buy debits cash, sell credits cash).

Short-side mechanics:
  - Opening a short credits the full sale proceeds to cash.
  - Requires SHORT_MARGIN_INITIAL × notional in available cash as initial margin.
  - Equity while short = cash − mark_price × short_qty  (covering costs mark × qty).
  - Not modeled: borrow fees (GLD/USO are easy-to-borrow; known optimism gap),
    margin maintenance / liquidation (risk manager ATR-limits sizes before
    orders reach here; intraday liquidation not required for 4-hour trend legs).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.models import Account, Order, OrderResult, Position, Side

SHORT_MARGIN_INITIAL = 0.50  # Reg-T initial margin fraction for short positions


class SimulatedBroker:
    """Immediate-fill broker with configurable slippage for walk-forward backtests."""

    def __init__(
        self,
        initial_equity: float = 100_000.0,
        slippage_pct: float = 0.0005,
        taker_fee_pct: float = 0.0,
    ) -> None:
        self.equity = initial_equity
        self.cash = initial_equity
        self.slippage_pct = slippage_pct
        # Per-side taker fee (e.g. crypto exchange fee), charged on every
        # fill in addition to slippage. Folded into the effective fill price
        # in the same direction as slippage: a buy pays more per share, a
        # sell receives less. Defaults to 0.0 so equity legs are unchanged.
        self.taker_fee_pct = taker_fee_pct
        self._positions: dict[str, Position] = {}
        self.fills: list[OrderResult] = []

    def _fill_price(self, side: Side, raw_price: float) -> float:
        slip = raw_price * (self.slippage_pct + self.taker_fee_pct)
        return raw_price + slip if side == Side.BUY else raw_price - slip

    def _open_long(self, symbol: str, qty: float, price: float) -> float:
        """Open or add to a long; returns filled qty (may be clipped by cash)."""
        if price <= 0:
            return 0.0
        affordable = int(self.cash / price)
        fill_qty = min(int(qty), affordable)
        if fill_qty <= 0:
            return 0.0

        cost = price * fill_qty
        pos = self._positions.get(symbol)
        if pos and pos.side == "long":
            total_qty = pos.qty + fill_qty
            avg = (pos.avg_entry_price * pos.qty + price * fill_qty) / total_qty
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=total_qty,
                side="long",
                avg_entry_price=avg,
            )
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=float(fill_qty),
                side="long",
                avg_entry_price=price,
            )
        self.cash -= cost
        return float(fill_qty)

    def _close_long(self, symbol: str, qty: float, price: float) -> float:
        """Close long shares; returns filled qty."""
        pos = self._positions.get(symbol)
        if not pos or pos.side != "long":
            return 0.0
        fill_qty = min(int(qty), int(pos.qty))
        if fill_qty <= 0:
            return 0.0
        self.cash += price * fill_qty
        remaining = pos.qty - fill_qty
        if remaining <= 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=remaining,
                side="long",
                avg_entry_price=pos.avg_entry_price,
            )
        return float(fill_qty)

    def _open_short(self, symbol: str, qty: float, price: float) -> float:
        """Open or add to a short; returns filled qty (clipped by margin requirement).

        Proceeds from the short sale are credited to cash immediately.
        Requires SHORT_MARGIN_INITIAL × notional in existing cash as margin.
        """
        if price <= 0:
            return 0.0
        margin_per_share = price * SHORT_MARGIN_INITIAL
        affordable = int(self.cash / margin_per_share) if margin_per_share > 0 else 0
        fill_qty = min(int(qty), affordable)
        if fill_qty <= 0:
            return 0.0

        pos = self._positions.get(symbol)
        if pos and pos.side == "short":
            total_qty = pos.qty + fill_qty
            avg = (pos.avg_entry_price * pos.qty + price * fill_qty) / total_qty
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=total_qty,
                side="short",
                avg_entry_price=avg,
            )
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=float(fill_qty),
                side="short",
                avg_entry_price=price,
            )
        self.cash += price * fill_qty  # receive short-sale proceeds
        return float(fill_qty)

    def _close_short(self, symbol: str, qty: float, price: float) -> float:
        """Cover short shares; returns filled qty."""
        pos = self._positions.get(symbol)
        if not pos or pos.side != "short":
            return 0.0
        fill_qty = min(int(qty), int(pos.qty))
        if fill_qty <= 0:
            return 0.0
        self.cash -= price * fill_qty  # pay to cover
        remaining = pos.qty - fill_qty
        if remaining <= 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=remaining,
                side="short",
                avg_entry_price=pos.avg_entry_price,
            )
        return float(fill_qty)

    def submit_orders(
        self, orders: list[Order], fill_price: float = 0.0
    ) -> list[OrderResult]:
        """Fill all orders immediately at `fill_price` ± slippage.

        BUY routes to _close_short if a short exists, otherwise _open_long.
        SELL routes to _close_long if a long exists, otherwise _open_short.
        Position state is updated order-by-order so a flip sequence
        (close existing + open opposite) works correctly within one call.
        """
        if fill_price <= 0:
            return []

        results: list[OrderResult] = []
        for order in orders:
            price = self._fill_price(order.side, fill_price)
            filled_qty = 0.0

            if order.side == Side.BUY:
                pos = self._positions.get(order.symbol)
                if pos and pos.side == "short":
                    filled_qty = self._close_short(order.symbol, order.qty, price)
                else:
                    filled_qty = self._open_long(order.symbol, order.qty, price)
            else:  # SELL
                pos = self._positions.get(order.symbol)
                if pos and pos.side == "long":
                    filled_qty = self._close_long(order.symbol, order.qty, price)
                else:
                    filled_qty = self._open_short(order.symbol, order.qty, price)

            if filled_qty <= 0:
                continue

            result = OrderResult(
                symbol=order.symbol,
                side=order.side,
                qty=filled_qty,
                filled_price=price,
                filled_at=datetime.now(timezone.utc),
                status="filled",
            )
            results.append(result)
            self.fills.append(result)

        self._mark_equity(fill_price)
        return results

    def _mark_equity(self, mark_price: float) -> None:
        """Mark all open positions to market and update total equity.

        Long: adds market value.
        Short: subtracts cover cost (equity = cash − mark × qty).
        """
        holdings = 0.0
        for pos in self._positions.values():
            if pos.side == "long":
                holdings += mark_price * pos.qty
            elif pos.side == "short":
                holdings -= mark_price * pos.qty
        self.equity = self.cash + holdings

    def get_positions(self) -> list[Position]:
        """Return a snapshot of open simulated positions."""
        return list(self._positions.values())

    def get_account(self) -> Account:
        """Return current simulated account state."""
        buying_power = max(self.cash, 0.0)
        return Account(
            equity=self.equity,
            cash=self.cash,
            buying_power=buying_power,
        )

    def close_all_positions(self, mark_price: float = 0.0) -> None:
        """Flatten all open positions at `mark_price` (circuit breaker parity)."""
        if mark_price <= 0:
            self._positions.clear()
            self.equity = self.cash
            return
        for symbol, pos in list(self._positions.items()):
            if pos.side == "long":
                self._close_long(symbol, pos.qty, self._fill_price(Side.SELL, mark_price))
            elif pos.side == "short":
                self._close_short(symbol, pos.qty, self._fill_price(Side.BUY, mark_price))
        self._mark_equity(mark_price)
