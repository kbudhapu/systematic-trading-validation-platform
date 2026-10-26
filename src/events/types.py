"""Event types published between decoupled pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.models import Bar, Order, OrderResult, Signal


@dataclass(frozen=True)
class BarsReceived:
    """Emitted by Data Ingestion when new bars are available."""

    symbol: str
    bars: tuple[Bar, ...]


@dataclass(frozen=True)
class SignalGenerated:
    """Emitted by Strategy Math when a signal is computed."""

    signal: Signal


@dataclass(frozen=True)
class OrdersRouted:
    """Emitted by Risk Management after sizing and validation."""

    orders: tuple[Order, ...]
    halted: bool = False
    halt_reason: str = ""


@dataclass(frozen=True)
class OrdersExecuted:
    """Emitted by Order Execution after broker submission."""

    results: tuple[OrderResult, ...]


@dataclass(frozen=True)
class CycleCompleted:
    """Emitted at end of each orchestrator cycle."""

    symbol: str
    metadata: dict[str, Any] | None = None
