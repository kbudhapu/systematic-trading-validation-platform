"""Per-strategy runtime state for the portfolio orchestrator."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from src.config import StrategyConfig
from src.core.rolling_window import RollingWindow
from src.models import Bar
from src.strategies.registry import leg_lookback_bars, leg_min_bars, leg_window_size
from src.strategies.base import BaseStrategy


@dataclass
class LegState:
    """Isolated window + strategy instance for one enabled leg."""

    config: StrategyConfig
    strategy: BaseStrategy
    window: RollingWindow
    strategy_uuid: str | None = None
    last_run_monotonic: float = 0.0
    # H1b (parity fix): timestamp of the last CLOSED bar this leg evaluated a signal on.
    # The poll timer (is_due) only CHECKS for a new closed bar; the signal is evaluated at
    # most ONCE per closed bar — never re-deciding the same unchanged bar ~15x/period (which
    # would be unreplayable, SFD 1.4). The backtest decides exactly once per bar.
    last_evaluated_bar_ts: datetime | None = None
    bars_in_trade: int = 0
    corporate_action_offset_session: str | None = None
    last_borrow_fee_session: str | None = None
    accrued_short_borrow_fees: float = 0.0

    def is_due(self, now: float) -> bool:
        return (now - self.last_run_monotonic) >= self.config.poll_interval_seconds

    def mark_ran(self, now: float | None = None) -> None:
        self.last_run_monotonic = now if now is not None else time.monotonic()

    def should_evaluate_closed_bar(self, latest_closed: Bar | None) -> bool:
        """H1b: evaluate a signal only when a NEW closed bar has arrived. The poll timer
        (is_due) fires many times per bar; without this guard the same unchanged closed bar
        would be re-decided ~15x/period — unreplayable (SFD 1.4) and unlike the backtest,
        which decides exactly once per bar."""
        if latest_closed is None:
            return False
        if self.last_evaluated_bar_ts is None:
            return True
        return latest_closed.timestamp > self.last_evaluated_bar_ts

    def mark_bar_evaluated(self, latest_closed: Bar) -> None:
        self.last_evaluated_bar_ts = latest_closed.timestamp


def leg_window_size_for(cfg: StrategyConfig) -> int:
    return leg_window_size(cfg.module, cfg.params)


def leg_lookback_bars_for(cfg: StrategyConfig) -> int:
    return leg_lookback_bars(cfg.module, cfg.params)


def leg_min_bars_for(cfg: StrategyConfig) -> int:
    return leg_min_bars(cfg.module, cfg.params)
