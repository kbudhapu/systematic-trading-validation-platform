"""
Domain models shared across all pipeline stages.

Immutable Bar for deque storage; mutable dataclasses for orders and portfolio state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    """Order side sent to the broker API."""

    BUY = "buy"
    SELL = "sell"


class SignalAction(str, Enum):
    """Strategy output consumed by the order router."""

    LONG = "long"
    SHORT = "short"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True)
class Bar:
    """Single OHLCV bar — immutable unit stored in the live deque window."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = ""


@dataclass
class Signal:
    """Actionable strategy output before risk sizing."""

    symbol: str
    action: SignalAction
    price: float
    timestamp: datetime
    strategy_id: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Order:
    """Sized order ready for broker submission."""

    symbol: str
    side: Side
    qty: float
    order_type: str = "market"
    stop_price: float | None = None
    strategy_id: str = ""
    client_order_id: str | None = None   # deterministic idempotency id (E1); None -> assigned at submit


@dataclass
class OrderResult:
    """Broker fill acknowledgement."""

    symbol: str
    side: Side
    qty: float
    filled_price: float
    filled_at: datetime
    order_id: str | None = None
    status: str = "filled"


@dataclass
class Position:
    """Open position snapshot from the broker."""

    symbol: str
    qty: float
    side: str
    avg_entry_price: float
    unrealized_pl: float = 0.0


@dataclass
class Account:
    """Account equity snapshot from the broker."""

    equity: float
    cash: float
    buying_power: float


@dataclass
class PortfolioState:
    """Tracks peak equity and circuit-breaker halt status."""

    peak_equity: float
    halted: bool = False
    halt_reason: str = ""
