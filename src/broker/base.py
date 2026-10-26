"""Async broker API protocol."""

from __future__ import annotations

from typing import Protocol

from src.models import Account, Order, OrderResult, Position


class BrokerAPI(Protocol):
    """Contract for order execution backends (Alpaca live, Simulated backtest)."""

    async def submit_orders(
        self, orders: list[Order], execution_contexts: dict[str, object] | None = None
    ) -> list[OrderResult]: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_account(self) -> Account: ...
    async def close_all_positions(self) -> None: ...
