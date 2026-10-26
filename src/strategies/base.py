"""
Strategy engine base class and registry.

Live path:  evaluate_live(RollingWindow) → deque + Numba
Batch path: generate_signals_batch(Polars) → Polars load + Numba scan
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import polars as pl

from src.core.rolling_window import RollingWindow
from src.math.indicators import EXIT, HOLD, LONG, SHORT
from src.models import Signal, SignalAction


class BaseStrategy(ABC):
    """Abstract strategy — every module in strategies/ implements this contract."""

    strategy_id: str = "base"

    @abstractmethod
    def evaluate_live(self, window: RollingWindow, params: dict) -> Signal | None:
        """Evaluate the latest bar using the live deque window (no Polars)."""

    @abstractmethod
    def generate_signals_batch(
        self,
        bars: pl.DataFrame,
        params: dict,
    ) -> list[Signal]:
        """Vectorised batch scan for research/backtest comparison reports."""

    def _action_code_to_signal(
        self,
        action_code: int,
        symbol: str,
        price: float,
        timestamp: datetime,
        metadata: dict | None = None,
    ) -> Signal | None:
        """Convert a Numba action code into a Signal dataclass."""
        mapping = {
            LONG: SignalAction.LONG,
            SHORT: SignalAction.SHORT,
            EXIT: SignalAction.EXIT,
        }
        if action_code == HOLD or action_code not in mapping:
            return None
        return Signal(
            symbol=symbol,
            action=mapping[action_code],
            price=price,
            timestamp=timestamp,
            strategy_id=self.strategy_id,
            metadata=metadata or {},
        )
