"""
Unified emergency flatten protocol — cancel, confirm, flatten cascade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from src.broker.execution_constants import LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS
from src.persistence import db as persistence
from src.persistence.governance_state_store import PendingOrderStore

log = structlog.get_logger()


class FlattenBroker(Protocol):
    async def get_positions(self) -> list[Any]: ...
    async def get_open_orders(self) -> list[Any]: ...
    async def cancel_open_orders_for_symbol(self, symbol: str) -> int: ...
    async def force_cancel_all_orders_for_symbol(self, symbol: str) -> int: ...
    async def await_open_orders_cleared(
        self, symbol: str, *, timeout_seconds: float
    ) -> tuple[bool, int]: ...
    async def force_flatten_symbol_position(
        self,
        symbol: str,
        *,
        position_side: str,
        position_qty: float,
    ) -> Any | None: ...
    async def close_all_positions(self) -> None: ...


@dataclass(frozen=True)
class FlattenResult:
    executed: bool
    skipped: bool
    reason: str
    symbols_flattened: tuple[str, ...] = ()
    symbols_failed: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None

    @property
    def partial_failure(self) -> bool:
        """True when at least one symbol was attempted but raised during flattening."""
        return len(self.symbols_failed) > 0


class UnifiedFlattenProtocol:
    """
    Single-writer flatten cascade:
    1) cancel pending orders
    2) await exchange clearance
    3) IOC/market exposure flatten
    """

    def __init__(
        self,
        broker: FlattenBroker,
        pending_orders: PendingOrderStore,
    ) -> None:
        self._broker = broker
        self._pending_orders = pending_orders
        self._portfolio_flattening = False
        self._leg_flattening: set[str] = set()

    @property
    def portfolio_flattening(self) -> bool:
        return self._portfolio_flattening

    def is_leg_flattening(self, strategy_id: str, symbol: str) -> bool:
        return f"{strategy_id}:{symbol.upper()}" in self._leg_flattening

    async def execute_portfolio_flatten(self, reason: str) -> FlattenResult:
        if self._portfolio_flattening:
            return FlattenResult(
                executed=False,
                skipped=True,
                reason="portfolio_flatten_already_active",
            )
        self._portfolio_flattening = True
        flattened_symbols: list[str] = []
        failed_symbols: list[str] = []
        failed_errors: dict[str, str] = {}
        try:
            positions = await self._broker.get_positions()
            symbols = sorted({str(p.symbol).upper() for p in positions})

            # Cancel loop — per-symbol isolation: one symbol's cancel failure
            # must not prevent the remaining symbols from being cancelled.
            for symbol in symbols:
                try:
                    cancelled = await self._broker.cancel_open_orders_for_symbol(symbol)
                    if cancelled:
                        log.warning(
                            "portfolio_flatten_cancelled_orders",
                            symbol=symbol,
                            cancelled=cancelled,
                        )
                except Exception as exc:
                    log.critical(
                        "portfolio_flatten_cancel_error",
                        symbol=symbol,
                        error=str(exc),
                    )

            # Await-clearance loop — per-symbol isolation.
            for symbol in symbols:
                try:
                    cleared, remaining = await self._broker.await_open_orders_cleared(
                        symbol,
                        timeout_seconds=LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
                    )
                    if not cleared:
                        forced = await self._broker.force_cancel_all_orders_for_symbol(symbol)
                        log.critical(
                            "portfolio_flatten_force_cancel",
                            symbol=symbol,
                            remaining_open_orders=remaining,
                            forced_cancelled=forced,
                        )
                        await self._broker.await_open_orders_cleared(
                            symbol,
                            timeout_seconds=LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
                        )
                except Exception as exc:
                    log.critical(
                        "portfolio_flatten_await_error",
                        symbol=symbol,
                        error=str(exc),
                    )

            self._pending_orders.clear_all()
            if positions:
                # Position-flatten loop — per-symbol isolation: a transient broker
                # error for symbol N must not leave symbols N+1 onward unflattened
                # in the same pass.  Any failure is recorded in failed_symbols and
                # surfaced in FlattenResult.symbols_failed so the caller can log a
                # critical alert and rely on the next-cycle recovery path for retry.
                for position in positions:
                    symbol = str(position.symbol).upper()
                    qty = float(getattr(position, "qty", 0.0) or 0.0)
                    if qty <= 0.0:
                        continue
                    side = str(getattr(position, "side", "flat") or "flat")
                    try:
                        result = await self._broker.force_flatten_symbol_position(
                            symbol,
                            position_side=side,
                            position_qty=qty,
                        )
                        if result is not None:
                            flattened_symbols.append(symbol)
                    except Exception as exc:
                        failed_symbols.append(symbol)
                        failed_errors[symbol] = str(exc)
                        log.critical(
                            "portfolio_flatten_symbol_failed",
                            symbol=symbol,
                            error=str(exc),
                            reason=reason,
                        )
            else:
                await self._broker.close_all_positions()

            persistence.log_system_event(
                "portfolio_flatten_executed",
                json.dumps(
                    {
                        "reason": reason,
                        "symbols": flattened_symbols,
                        "symbols_failed": failed_symbols,
                        "position_count": len(positions),
                    },
                    separators=(",", ":"),
                ),
                severity="critical",
            )
            return FlattenResult(
                executed=True,
                skipped=False,
                reason=reason,
                symbols_flattened=tuple(flattened_symbols),
                symbols_failed=tuple(failed_symbols),
                metadata={
                    "position_count": len(positions),
                    "failed_errors": failed_errors if failed_errors else None,
                },
            )
        finally:
            self._portfolio_flattening = False

    async def execute_leg_flatten(
        self,
        *,
        strategy_id: str,
        symbol: str,
        position_side: str,
        position_qty: float,
        reason: str,
    ) -> bool:
        leg_key = f"{strategy_id}:{symbol.upper()}"
        if leg_key in self._leg_flattening:
            log.warning("leg_flatten_skipped_duplicate", leg=leg_key, reason=reason)
            return False
        self._leg_flattening.add(leg_key)
        try:
            cancelled = await self._broker.cancel_open_orders_for_symbol(symbol)
            removed = self._pending_orders.clear_prefix(strategy_id, symbol)
            if cancelled or removed:
                log.warning(
                    "leg_flatten_cancelled_orders",
                    leg=strategy_id,
                    symbol=symbol.upper(),
                    broker_cancelled=cancelled,
                    staged_removed=removed,
                )
            cleared, remaining = await self._broker.await_open_orders_cleared(
                symbol.upper(),
                timeout_seconds=LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
            )
            if not cleared and remaining > 0:
                forced = await self._broker.force_cancel_all_orders_for_symbol(symbol)
                log.critical(
                    "leg_flatten_force_cancel",
                    leg=strategy_id,
                    symbol=symbol.upper(),
                    remaining_open_orders=remaining,
                    forced_cancelled=forced,
                )
                cleared, remaining = await self._broker.await_open_orders_cleared(
                    symbol.upper(),
                    timeout_seconds=LIQUIDATION_CANCEL_CONFIRM_TIMEOUT_SECONDS,
                )
            if position_qty > 0.0 and position_side in {"long", "short"}:
                flatten_result = await self._broker.force_flatten_symbol_position(
                    symbol,
                    position_side=position_side,
                    position_qty=position_qty,
                )
                if flatten_result is None:
                    log.critical(
                        "leg_flatten_no_position",
                        leg=strategy_id,
                        symbol=symbol.upper(),
                    )
                    return cleared
            persistence.log_system_event(
                "leg_flatten_executed",
                json.dumps(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol.upper(),
                        "reason": reason,
                        "remaining_open_orders": remaining,
                    },
                    separators=(",", ":"),
                ),
                severity="critical",
            )
            return cleared
        finally:
            self._leg_flattening.discard(leg_key)
