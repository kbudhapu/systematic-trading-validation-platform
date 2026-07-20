"""
Alpaca order execution — async wrapper around the REST trading client.

All network calls run via asyncio.to_thread to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.common.enums import Sort
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from src.ingestor.assets import crypto_data_symbol, crypto_order_symbol, infer_asset_class
from src.models import Account, Order, OrderResult, Position, Side
from src.router.risk_manager import ExecutionDriftDiagnostics

log = structlog.get_logger()

IOC_LIMIT_MULT = 2.0
PASSIVE_LIMIT_MULT = 0.5
TWAP_SLICES = 3
TWAP_SLEEP_SECONDS = 2.0
ORDER_STATUS_POLL_ATTEMPTS = 5
ORDER_STATUS_POLL_SLEEP_SECONDS = 0.75
SHORT_POLL_ATTEMPTS = 3
SHORT_POLL_SLEEP_SECONDS = 0.1
TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "partially_filled", "canceled", "cancelled", "expired", "rejected"}
)
from src.broker.execution_constants import (
    LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
    LIQUIDATION_CANCEL_POLL_INTERVAL_SECONDS,
)


@dataclass(frozen=True)
class ExecutionTacticContext:
    strategy_id: str
    symbol: str
    reference_price: float
    modeled_slippage_pct: float
    diagnostics: ExecutionDriftDiagnostics
    force_aggressive_ioc: bool = False


@dataclass(frozen=True)
class NbboSnapshot:
    symbol: str
    bid_price: float
    ask_price: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    source: str = "rest"

    @property
    def mid_price(self) -> float:
        if self.bid_price > 0.0 and self.ask_price > 0.0:
            return (self.bid_price + self.ask_price) / 2.0
        return max(self.bid_price, self.ask_price, 0.0)

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid <= 0.0:
            return 0.0
        return max(self.ask_price - self.bid_price, 0.0) / mid

    @property
    def total_depth(self) -> float:
        return max(self.bid_size, 0.0) + max(self.ask_size, 0.0)


@dataclass(frozen=True)
class BrokerOpenOrder:
    order_id: str
    symbol: str
    side: str
    qty: float
    status: str
    order_type: str = "unknown"


def is_pending_exit_order(order: BrokerOpenOrder, *, position_side: str) -> bool:
    side = str(order.side).strip().lower()
    normalized_position = str(position_side).strip().lower()
    if normalized_position == "long":
        return side == "sell"
    if normalized_position == "short":
        return side == "buy"
    return True


@dataclass(frozen=True)
class BrokerFillSnapshot:
    order_id: str
    symbol: str
    side: str
    filled_qty: float
    filled_at: datetime


def _context_key(order: Order) -> str:
    return f"{order.strategy_id}:{order.symbol}:{order.side.value}"


class AlpacaBroker:
    """Submit and query orders on Alpaca paper or live accounts."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        self._client = TradingClient(api_key, secret_key, paper=paper)
        self._market_data = StockHistoricalDataClient(api_key, secret_key)
        self._crypto_market_data = CryptoHistoricalDataClient(api_key, secret_key)

    @staticmethod
    def _soak_mode_enabled() -> bool:
        return os.getenv("SOAK_TEST_MODE", "").strip() == "1"

    def _symbol_and_tif(self, symbol: str) -> tuple[str, TimeInForce]:
        tif = TimeInForce.DAY
        if infer_asset_class(symbol) == "crypto":
            return crypto_order_symbol(symbol), TimeInForce.GTC
        return symbol, tif

    def _limit_price(
        self,
        side: Side,
        reference_price: float,
        slippage_pct: float,
        aggressiveness: float,
    ) -> float:
        slip = max(reference_price * slippage_pct * aggressiveness, 0.01)
        if side == Side.BUY:
            return round(reference_price + slip, 2)
        return round(max(0.01, reference_price - slip), 2)

    def _poll_order_by_id_sync(self, order_id: str) -> tuple[float, float, str]:
        polled = self._client.get_order_by_id(order_id)
        status = str(polled.status)
        filled_avg = float(polled.filled_avg_price or 0.0)
        filled_qty = float(polled.filled_qty or 0.0)
        return filled_avg, filled_qty, status

    def _wait_for_fill_sync(self, order_id: str) -> tuple[float, float, str]:
        for _ in range(SHORT_POLL_ATTEMPTS):
            try:
                filled_avg, filled_qty, status = self._poll_order_by_id_sync(order_id)
                if (
                    filled_qty > 0.0
                    or status.lower() in TERMINAL_ORDER_STATUSES
                ):
                    return filled_avg, filled_qty, status
            except Exception as e:
                log.warning("order_poll_failed", order_id=order_id, error=str(e))
                break
            time.sleep(SHORT_POLL_SLEEP_SECONDS)
        for _ in range(ORDER_STATUS_POLL_ATTEMPTS):
            try:
                filled_avg, filled_qty, status = self._poll_order_by_id_sync(order_id)
                if (
                    filled_avg > 0.0
                    or filled_qty > 0.0
                    or status.lower() in TERMINAL_ORDER_STATUSES
                ):
                    return filled_avg, filled_qty, status
            except Exception as e:
                log.warning("order_poll_failed", order_id=order_id, error=str(e))
                break
            time.sleep(ORDER_STATUS_POLL_SLEEP_SECONDS)
        return 0.0, 0.0, "submitted"

    def _resolved_fill_qty(self, order: Order, filled_qty: float, status: str) -> float:
        if filled_qty > 0.0:
            return filled_qty
        normalized = status.lower()
        if normalized in {"rejected", "expired", "canceled", "cancelled"}:
            return 0.0
        return float(order.qty)

    def _submit_market_sync(self, order: Order) -> OrderResult:
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        symbol, tif = self._symbol_and_tif(order.symbol)
        req = MarketOrderRequest(
            symbol=symbol,
            qty=order.qty,
            side=side,
            time_in_force=tif,
            client_order_id=order.client_order_id,   # E1: deterministic idempotency id
        )
        submitted = self._client.submit_order(req)
        filled_price, filled_qty, status = self._wait_for_fill_sync(str(submitted.id))
        return OrderResult(
            symbol=order.symbol,
            side=order.side,
            qty=self._resolved_fill_qty(order, filled_qty, status),
            filled_price=filled_price,
            filled_at=datetime.now(timezone.utc),
            order_id=str(submitted.id),
            status=status,
        )

    def get_order_by_client_order_id_sync(self, client_order_id: str):
        """Return the broker order for a client_order_id, or None (E1 dedupe query)."""
        try:
            return self._client.get_order_by_client_order_id(client_order_id)
        except Exception:
            return None

    async def get_order_by_client_order_id(self, client_order_id: str):
        return await asyncio.to_thread(self.get_order_by_client_order_id_sync, client_order_id)

    def _submit_ioc_limit_sync(
        self, order: Order, context: ExecutionTacticContext
    ) -> OrderResult:
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        symbol, _ = self._symbol_and_tif(order.symbol)
        limit_price = self._limit_price(
            order.side,
            context.reference_price,
            context.modeled_slippage_pct,
            IOC_LIMIT_MULT,
        )
        req = LimitOrderRequest(
            symbol=symbol,
            qty=order.qty,
            side=side,
            time_in_force=TimeInForce.IOC,
            limit_price=limit_price,
        )
        submitted = self._client.submit_order(req)
        filled_price, filled_qty, status = self._wait_for_fill_sync(str(submitted.id))
        return OrderResult(
            symbol=order.symbol,
            side=order.side,
            qty=self._resolved_fill_qty(order, filled_qty, status),
            filled_price=filled_price,
            filled_at=datetime.now(timezone.utc),
            order_id=str(submitted.id),
            status=status,
        )

    def _get_nbbo_snapshot_sync(self, symbol: str) -> NbboSnapshot | None:
        if infer_asset_class(symbol) != "stock":
            return None
        try:
            # N1: the NBBO snapshot IS the fill/price reference — slippage = fill - reference.
            # It must read the SAME feed as the signal bars, never the alpaca-py default (IEX);
            # an IEX quote on a SIP-decided trade calibrates Stage-5 against a market we do not
            # trade in (F4), baked into the very first fill.
            from src.ingestor.market_data_feed import soak_market_data_feed

            request = StockLatestQuoteRequest(
                symbol_or_symbols=symbol, feed=soak_market_data_feed()
            )
            response = self._market_data.get_stock_latest_quote(request)
            quote = None
            if isinstance(response, dict):
                quote = response.get(symbol)
            else:
                data = getattr(response, "data", None)
                if isinstance(data, dict):
                    quote = data.get(symbol)
                elif data is not None:
                    quote = getattr(data, symbol, None)
            if quote is None:
                return None
            bid_price = float(getattr(quote, "bid_price", 0.0) or 0.0)
            ask_price = float(getattr(quote, "ask_price", 0.0) or 0.0)
            bid_size = float(getattr(quote, "bid_size", 0.0) or 0.0)
            ask_size = float(getattr(quote, "ask_size", 0.0) or 0.0)
            if bid_price <= 0.0 and ask_price <= 0.0:
                return None
            return NbboSnapshot(
                symbol=symbol,
                bid_price=bid_price,
                ask_price=ask_price,
                bid_size=bid_size,
                ask_size=ask_size,
                source="rest",
            )
        except Exception as e:
            log.warning("nbbo_snapshot_failed", symbol=symbol, error=str(e))
            return None

    def _get_crypto_nbbo_snapshot_sync(self, symbol: str) -> NbboSnapshot | None:
        if infer_asset_class(symbol) != "crypto":
            return None
        try:
            data_symbol = crypto_data_symbol(symbol)
            request = CryptoLatestQuoteRequest(symbol_or_symbols=data_symbol)
            response = self._crypto_market_data.get_crypto_latest_quote(request)
            quote = None
            if isinstance(response, dict):
                quote = response.get(data_symbol)
            else:
                data = getattr(response, "data", None)
                if isinstance(data, dict):
                    quote = data.get(data_symbol)
                elif data is not None:
                    quote = getattr(data, data_symbol, None)
            if quote is None:
                return None
            bid_price = float(getattr(quote, "bid_price", 0.0) or 0.0)
            ask_price = float(getattr(quote, "ask_price", 0.0) or 0.0)
            bid_size = float(getattr(quote, "bid_size", 0.0) or 0.0)
            ask_size = float(getattr(quote, "ask_size", 0.0) or 0.0)
            if bid_price <= 0.0 and ask_price <= 0.0:
                return None
            return NbboSnapshot(
                symbol=symbol,
                bid_price=bid_price,
                ask_price=ask_price,
                bid_size=bid_size,
                ask_size=ask_size,
                source="rest_crypto",
            )
        except Exception as e:
            log.warning("crypto_nbbo_snapshot_failed", symbol=symbol, error=str(e))
            return None

    def get_nbbo_snapshot(self, symbol: str) -> NbboSnapshot | None:
        """Route a symbol to its correct quote source: the equity consolidated NBBO for stocks, the
        crypto latest-quote for crypto. The right data source is a PROPERTY OF THE INSTRUMENT, resolved
        HERE — callers ask for "the quote" and never branch on asset class. Before this, callers hit the
        equity method directly, which returns None for crypto (BTC has no consolidated equity NBBO), and
        a None was then miscounted as an NBBO fetch FAILURE — pinning the crypto leg in HARD_CRITICAL
        even though a real crypto quote (get_crypto_latest_quote) was available the whole time."""
        if infer_asset_class(symbol) == "crypto":
            return self._get_crypto_nbbo_snapshot_sync(symbol)
        return self._get_nbbo_snapshot_sync(symbol)

    def _twap_limit_from_nbbo(
        self,
        order: Order,
        context: ExecutionTacticContext,
        snapshot: NbboSnapshot,
    ) -> float:
        slip = max(snapshot.mid_price * context.modeled_slippage_pct * PASSIVE_LIMIT_MULT, 0.01)
        if order.side == Side.BUY:
            anchor = snapshot.bid_price if snapshot.bid_price > 0.0 else snapshot.mid_price
            return round(max(0.01, anchor + slip), 2)
        anchor = snapshot.ask_price if snapshot.ask_price > 0.0 else snapshot.mid_price
        return round(max(0.01, anchor - slip), 2)

    def _submit_passive_twap_sync(
        self, order: Order, context: ExecutionTacticContext
    ) -> list[OrderResult]:
        # Crypto instruments cannot use TWAP:
        # (1) _get_nbbo_snapshot_sync() returns None for non-stock symbols, so there
        #     is no NBBO-anchored passive price — TWAP would fall back to a plain
        #     reference-price limit every slice, adding latency with no benefit.
        # (2) _symbol_and_tif() returns TimeInForce.GTC for crypto, so each slice
        #     would remain open indefinitely with no inter-slice cancellation.
        # (3) The int() slice-qty floor is wrong for fractional quantities.
        # Route crypto as a single IOC limit order instead (same as the critical path).
        if infer_asset_class(order.symbol) == "crypto":
            return [self._submit_ioc_limit_sync(order, context)]

        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        symbol, tif = self._symbol_and_tif(order.symbol)
        slice_qty = max(1.0, float(int(max(order.qty / TWAP_SLICES, 1.0))))
        remaining = float(order.qty)
        results: list[OrderResult] = []
        while remaining > 0:
            child_qty = min(slice_qty, remaining)
            snapshot = self._get_nbbo_snapshot_sync(order.symbol)
            if snapshot is not None:
                limit_price = self._twap_limit_from_nbbo(order, context, snapshot)
                log.info(
                    "twap_nbbo_anchor",
                    symbol=order.symbol,
                    bid_price=snapshot.bid_price,
                    ask_price=snapshot.ask_price,
                    mid_price=snapshot.mid_price,
                    side=order.side.value,
                    limit_price=limit_price,
                )
            else:
                limit_price = self._limit_price(
                    order.side,
                    context.reference_price,
                    context.modeled_slippage_pct,
                    PASSIVE_LIMIT_MULT,
                )
                log.warning(
                    "twap_nbbo_fallback",
                    symbol=order.symbol,
                    side=order.side.value,
                    fallback_price=limit_price,
                )
            req = LimitOrderRequest(
                symbol=symbol,
                qty=child_qty,
                side=side,
                time_in_force=tif,
                limit_price=limit_price,
            )
            submitted = self._client.submit_order(req)
            filled_price, filled_qty, status = self._wait_for_fill_sync(str(submitted.id))
            slice_order = Order(symbol=order.symbol, side=order.side, qty=child_qty)
            results.append(
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    qty=self._resolved_fill_qty(slice_order, filled_qty, status),
                    filled_price=filled_price,
                    filled_at=datetime.now(timezone.utc),
                    order_id=str(submitted.id),
                    status=status,
                )
            )
            remaining -= child_qty
            if remaining > 0:
                time.sleep(TWAP_SLEEP_SECONDS)
        return results

    def _submit_sync(
        self,
        orders: list[Order],
        execution_contexts: dict[str, ExecutionTacticContext] | None = None,
    ) -> list[OrderResult]:
        """Blocking order submission — called via asyncio.to_thread."""
        results: list[OrderResult] = []
        for order in orders:
            try:
                context = None
                if execution_contexts is not None:
                    context = execution_contexts.get(_context_key(order))

                if context is None:
                    result_batch = [self._submit_market_sync(order)]
                    tactic = "market_fallback"
                elif order.order_type == "market":
                    result_batch = [self._submit_market_sync(order)]
                    tactic = "market_forced_liquidation"
                elif context.force_aggressive_ioc:
                    result_batch = [self._submit_ioc_limit_sync(order, context)]
                    tactic = "defensive_ioc_depth_fallback"
                elif context.diagnostics.elevated and not context.diagnostics.critical:
                    result_batch = self._submit_passive_twap_sync(order, context)
                    tactic = "passive_twap"
                else:
                    result_batch = [self._submit_ioc_limit_sync(order, context)]
                    tactic = "aggressive_ioc_limit"

                results.extend(result_batch)
                log.info(
                    "order_submitted",
                    symbol=order.symbol,
                    qty=order.qty,
                    side=order.side.value,
                    tactic=tactic,
                    avg_slip=context.diagnostics.average_realized_slippage_pct
                    if context is not None
                    else None,
                    drift_multiple=context.diagnostics.drift_multiple
                    if context is not None
                    else None,
                )
            except Exception as e:
                log.error("order_failed", symbol=order.symbol, error=str(e))
                results.append(
                    OrderResult(
                        symbol=order.symbol,
                        side=order.side,
                        qty=0.0,
                        filled_price=0.0,
                        filled_at=datetime.now(timezone.utc),
                        order_id=None,
                        status="submission_failed",
                    )
                )
        return results

    def _get_symbol_fills_sync(
        self,
        symbol: str,
        *,
        lookback_days: int = 30,
    ) -> list[BrokerFillSnapshot]:
        symbol_key = symbol.upper()
        alpaca_symbol, _ = self._symbol_and_tif(symbol_key)
        after = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[alpaca_symbol],
            after=after,
            direction=Sort.DESC,
            limit=500,
        )
        orders = self._client.get_orders(filter=request)
        fills: list[BrokerFillSnapshot] = []
        for order in orders:
            filled_at = order.filled_at
            filled_qty = float(order.filled_qty or 0.0)
            if filled_at is None or filled_qty <= 0.0:
                continue
            status = order.status
            if status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
                continue
            if str(order.symbol).upper() != symbol_key:
                continue
            fills.append(
                BrokerFillSnapshot(
                    order_id=str(order.id),
                    symbol=symbol_key,
                    side=str(order.side.value).lower(),
                    filled_qty=filled_qty,
                    filled_at=filled_at,
                )
            )
        fills.sort(key=lambda fill: fill.filled_at)
        return fills

    def _get_open_orders_sync(self) -> list[BrokerOpenOrder]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._client.get_orders(filter=request)
        snapshots: list[BrokerOpenOrder] = []
        for order in orders:
            snapshots.append(
                BrokerOpenOrder(
                    order_id=str(order.id),
                    symbol=str(order.symbol).upper(),
                    side=str(order.side.value).lower(),
                    qty=float(order.qty),
                    status=str(order.status.value).lower(),
                    order_type=str(getattr(order.type, "value", order.type)).lower(),
                )
            )
        return snapshots

    def _get_open_orders_for_symbol_sync(self, symbol: str) -> list[BrokerOpenOrder]:
        symbol_key = symbol.upper()
        return [
            order
            for order in self._get_open_orders_sync()
            if order.symbol == symbol_key
        ]

    def _count_open_orders_for_symbol_sync(self, symbol: str) -> int:
        symbol_key = symbol.upper()
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = self._client.get_orders(filter=request)
        return sum(
            1 for order in open_orders if str(order.symbol).upper() == symbol_key
        )

    def _cancel_open_orders_for_symbol_sync(self, symbol: str) -> int:
        symbol_key = symbol.upper()
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = self._client.get_orders(filter=request)
        cancelled = 0
        for order in open_orders:
            if str(order.symbol).upper() != symbol_key:
                continue
            self._client.cancel_order_by_id(order.id)
            cancelled += 1
        return cancelled

    def _force_cancel_all_orders_for_symbol_sync(self, symbol: str) -> int:
        symbol_key = symbol.upper()
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = self._client.get_orders(filter=request)
        cancelled = 0
        for order in open_orders:
            if str(order.symbol).upper() != symbol_key:
                continue
            try:
                self._client.cancel_order_by_id(order.id)
                cancelled += 1
            except Exception as exc:
                log.warning(
                    "force_cancel_order_failed",
                    symbol=symbol_key,
                    order_id=str(order.id),
                    error=str(exc),
                )
        return cancelled

    def _force_flatten_symbol_sync(
        self,
        symbol: str,
        *,
        position_side: str,
        position_qty: float,
    ) -> OrderResult | None:
        qty = float(position_qty)
        if qty <= 0.0:
            return None
        normalized_side = str(position_side).strip().lower()
        close_side = Side.SELL if normalized_side == "long" else Side.BUY
        order = Order(
            symbol=symbol,
            side=close_side,
            qty=qty,
            order_type="market",
            strategy_id="forced_liquidation_recovery",
        )
        if infer_asset_class(symbol) == "stock":
            nbbo = self._get_nbbo_snapshot_sync(symbol)
            if nbbo is not None and nbbo.mid_price > 0.0:
                context = ExecutionTacticContext(
                    strategy_id="forced_liquidation_recovery",
                    symbol=symbol,
                    reference_price=nbbo.mid_price,
                    modeled_slippage_pct=0.001,
                    diagnostics=ExecutionDriftDiagnostics(
                        average_realized_slippage_pct=0.001,
                        modeled_slippage_pct=0.001,
                        drift_multiple=1.0,
                        elevated=True,
                        critical=True,
                    ),
                )
                ioc_result = self._submit_ioc_limit_sync(order, context)
                if ioc_result.status in {"filled", "partially_filled"} and ioc_result.filled_price > 0.0:
                    return ioc_result
        return self._submit_market_sync(order)

    def _get_positions_sync(self) -> list[Position]:
        """Blocking position fetch — called via asyncio.to_thread."""
        positions = self._client.get_all_positions()
        result = []
        for p in positions:
            qty = float(p.qty)
            result.append(
                Position(
                    symbol=p.symbol,
                    qty=abs(qty),
                    side="long" if qty > 0 else "short",
                    avg_entry_price=float(p.avg_entry_price),
                    unrealized_pl=float(p.unrealized_pl),
                )
            )
        return result

    def _get_account_sync(self) -> Account:
        """Blocking account fetch — called via asyncio.to_thread."""
        acct = self._client.get_account()
        return Account(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
        )

    def _close_all_sync(self) -> None:
        """Blocking close-all — called via asyncio.to_thread."""
        self._client.close_all_positions(cancel_orders=True)

    async def submit_orders(
        self,
        orders: list[Order],
        execution_contexts: dict[str, ExecutionTacticContext] | None = None,
    ) -> list[OrderResult]:
        """Submit live orders asynchronously using microstructure-aware tactics."""
        if self._soak_mode_enabled():
            now = datetime.now(timezone.utc)
            log.warning("soak_test_order_submit_blocked", orders=len(orders))
            return [
                OrderResult(
                    symbol=order.symbol,
                    side=order.side,
                    qty=0.0,
                    filled_price=0.0,
                    filled_at=now,
                    order_id=f"soak-sim-{idx}",
                    status="soak_blocked",
                )
                for idx, order in enumerate(orders)
            ]
        return await asyncio.to_thread(self._submit_sync, orders, execution_contexts)

    async def refresh_order_result(
        self,
        order: Order,
        result: OrderResult,
    ) -> OrderResult:
        """Re-poll a single broker order and return an updated fill snapshot."""
        if not result.order_id:
            return result
        filled_price, filled_qty, status = await asyncio.to_thread(
            self._poll_order_by_id_sync,
            result.order_id,
        )
        return OrderResult(
            symbol=result.symbol,
            side=result.side,
            qty=self._resolved_fill_qty(order, filled_qty, status),
            filled_price=filled_price,
            filled_at=result.filled_at,
            order_id=result.order_id,
            status=status,
        )

    async def short_burst_refresh_order_results(
        self,
        orders: list[Order],
        results: list[OrderResult],
    ) -> list[OrderResult]:
        """Execute immediate post-submit status polls (3 x 100ms) per child order."""
        if not results:
            return results
        order_lookup = {
            f"{order.symbol}:{order.side.value}": order for order in orders
        }
        refreshed: list[OrderResult] = []
        for result in results:
            order = order_lookup.get(f"{result.symbol}:{result.side.value}")
            if order is None and orders:
                order = orders[0]
            if order is None or not result.order_id:
                refreshed.append(result)
                continue
            filled_price = float(result.filled_price)
            filled_qty = 0.0
            status = str(result.status)
            for _ in range(SHORT_POLL_ATTEMPTS):
                filled_price, filled_qty, status = await asyncio.to_thread(
                    self._poll_order_by_id_sync,
                    result.order_id,
                )
                if filled_qty > 0.0 or status.lower() in TERMINAL_ORDER_STATUSES:
                    break
                await asyncio.sleep(SHORT_POLL_SLEEP_SECONDS)
            refreshed.append(
                OrderResult(
                    symbol=result.symbol,
                    side=result.side,
                    qty=self._resolved_fill_qty(order, filled_qty, status),
                    filled_price=filled_price,
                    filled_at=result.filled_at,
                    order_id=result.order_id,
                    status=status,
                )
            )
        return refreshed

    async def get_open_orders(self) -> list[BrokerOpenOrder]:
        """Return broker open orders asynchronously."""
        return await asyncio.to_thread(self._get_open_orders_sync)

    async def get_symbol_fills(
        self,
        symbol: str,
        *,
        lookback_days: int = 30,
    ) -> list[BrokerFillSnapshot]:
        """Return filled orders for a symbol, oldest first."""
        return await asyncio.to_thread(
            self._get_symbol_fills_sync,
            symbol,
            lookback_days=lookback_days,
        )

    async def count_open_orders_for_symbol(self, symbol: str) -> int:
        """Return the number of broker-open orders for a symbol."""
        return await asyncio.to_thread(
            self._count_open_orders_for_symbol_sync,
            symbol.upper(),
        )

    async def await_open_orders_cleared(
        self,
        symbol: str,
        *,
        timeout_seconds: float = LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
        poll_interval_seconds: float = LIQUIDATION_CANCEL_POLL_INTERVAL_SECONDS,
    ) -> tuple[bool, int]:
        """
        Poll broker open-order state until the symbol has zero working orders
        or the timeout elapses.
        """
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        symbol_key = symbol.upper()
        remaining = await self.count_open_orders_for_symbol(symbol_key)
        if remaining == 0:
            return True, 0
        while time.monotonic() < deadline:
            await asyncio.sleep(max(poll_interval_seconds, 0.05))
            remaining = await self.count_open_orders_for_symbol(symbol_key)
            if remaining == 0:
                return True, 0
        return False, remaining

    async def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all open broker orders for a symbol before forced liquidation."""
        return await asyncio.to_thread(
            self._cancel_open_orders_for_symbol_sync,
            symbol.upper(),
        )

    async def get_open_orders_for_symbol(self, symbol: str) -> list[BrokerOpenOrder]:
        return await asyncio.to_thread(
            self._get_open_orders_for_symbol_sync,
            symbol.upper(),
        )

    async def force_cancel_all_orders_for_symbol(self, symbol: str) -> int:
        return await asyncio.to_thread(
            self._force_cancel_all_orders_for_symbol_sync,
            symbol.upper(),
        )

    async def force_flatten_symbol_position(
        self,
        symbol: str,
        *,
        position_side: str,
        position_qty: float,
    ) -> OrderResult | None:
        if self._soak_mode_enabled():
            log.warning(
                "soak_test_force_flatten_blocked",
                symbol=symbol.upper(),
                position_side=position_side,
                position_qty=position_qty,
            )
            return None
        return await asyncio.to_thread(
            self._force_flatten_symbol_sync,
            symbol,
            position_side=position_side,
            position_qty=position_qty,
        )

    async def recover_forced_liquidation_cancel_timeout(
        self,
        symbol: str,
        *,
        position_side: str,
        position_qty: float,
    ) -> tuple[bool, str]:
        open_orders = await self.get_open_orders_for_symbol(symbol)
        if not open_orders:
            return True, "orders_cleared_before_recovery"
        if not all(
            is_pending_exit_order(order, position_side=position_side)
            for order in open_orders
        ):
            return False, "non_exit_orders_blocking_liquidation"
        cancelled = await self.force_cancel_all_orders_for_symbol(symbol)
        flatten_result = await self.force_flatten_symbol_position(
            symbol,
            position_side=position_side,
            position_qty=position_qty,
        )
        if flatten_result is None:
            return False, "no_position_to_flatten"
        flatten_tactic = (
            "ioc_limit"
            if flatten_result.status in {"filled", "partially_filled"}
            and flatten_result.filled_price > 0.0
            else "market_fallback"
        )
        log.critical(
            "forced_liquidation_recovery_executed",
            symbol=symbol.upper(),
            cancelled_orders=cancelled,
            flatten_tactic=flatten_tactic,
            flatten_status=flatten_result.status,
            flatten_qty=flatten_result.qty,
        )
        return True, f"forced_cancel_and_{flatten_tactic}_flatten"

    async def get_positions(self) -> list[Position]:
        """Return open positions asynchronously."""
        return await asyncio.to_thread(self._get_positions_sync)

    async def get_account(self) -> Account:
        """Return account equity snapshot asynchronously."""
        return await asyncio.to_thread(self._get_account_sync)

    async def close_all_positions(self) -> None:
        """Flatten all positions — used by the drawdown circuit breaker."""
        if self._soak_mode_enabled():
            log.warning("soak_test_close_all_blocked")
            return
        await asyncio.to_thread(self._close_all_sync)
        log.warning("all_positions_closed")
