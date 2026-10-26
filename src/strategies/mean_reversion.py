"""
Mean reversion strategy — fade statistically extended moves back to the SMA.

SPY: 1.5σ threshold (15-min). QQQ: 1.8σ (V3).
"""

from __future__ import annotations

import polars as pl

from src.core.rolling_window import RollingWindow
from src.math.indicators import (
    mean_reversion_action,
    rolling_sma,
    rolling_std,
    scan_mean_reversion_signals,
)
from src.models import Signal
from src.strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Z-score mean reversion on a rolling SMA — Numba-accelerated."""

    strategy_id = "mean_reversion"

    def evaluate_live(self, window: RollingWindow, params: dict) -> Signal | None:
        """
        Evaluate the current bar from the live deque window.

        Uses tail_closes() to minimise array copy size on the hot path.
        """
        sma_period = params.get("sma_period", 20)
        threshold = params.get("threshold_sigma", 1.5)
        exit_sigma = params.get("exit_sigma", 0.1)
        symbol = params.get("symbol", "SPY")

        if not window.is_ready(sma_period):
            return None

        closes = window.tail_closes(sma_period)
        sma = rolling_sma(closes, sma_period)
        std = rolling_std(closes, sma_period)
        latest = window.latest()
        if latest is None:
            return None

        action_code = mean_reversion_action(
            latest.close, sma, std, threshold, exit_sigma
        )
        z = (latest.close - sma) / std if std > 0 else 0.0
        return self._action_code_to_signal(
            action_code,
            symbol,
            latest.close,
            latest.timestamp,
            metadata={"z_score": z, "sma": sma},
        )

    def generate_signals_batch(
        self,
        bars: pl.DataFrame,
        params: dict,
    ) -> list[Signal]:
        """
        Batch signal scan for research — Polars loads data, Numba scans math.

        Not used in the live event loop.
        """
        if bars.is_empty():
            return []

        sma_period = params.get("sma_period", 20)
        threshold = params.get("threshold_sigma", 1.5)
        exit_sigma = params.get("exit_sigma", 0.1)
        symbol = params.get(
            "symbol",
            bars["symbol"][0] if "symbol" in bars.columns else "SPY",
        )

        sorted_bars = bars.sort("timestamp")
        closes = sorted_bars["close"].to_numpy().astype("float64")
        timestamps = sorted_bars["timestamp"].to_list()

        action_codes = scan_mean_reversion_signals(
            closes, sma_period, threshold, exit_sigma
        )

        signals: list[Signal] = []
        for i, code in enumerate(action_codes):
            if code == 0:
                continue
            sig = self._action_code_to_signal(
                int(code),
                symbol,
                float(closes[i]),
                timestamps[i],
            )
            if sig:
                signals.append(sig)
        return signals
