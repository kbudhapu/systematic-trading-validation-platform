"""Order router — bridges strategy signals and broker execution via risk checks."""

from __future__ import annotations

from typing import Any

from src.config import RiskConfig
from src.core.rolling_window import RollingWindow
from src.models import Account, Order, PortfolioState, Position, Signal
from src.router.risk_manager import (
    AlpacaAssetContext,
    RiskManager,
    check_drawdown_breaker,
)


class OrderRouter:
    """Applies drawdown breaker then delegates sizing to RiskManager."""

    def __init__(self, risk_config: RiskConfig) -> None:
        self.risk_manager = RiskManager(risk_config)

    def route(
        self,
        signal: Signal | None,
        window: RollingWindow,
        account: Account,
        positions: list[Position],
        portfolio_state: PortfolioState,
        risk_budget_fraction: float = 1.0,
        strategy_params: dict[str, Any] | None = None,
        asset_context: AlpacaAssetContext | None = None,
        active_windows: dict[str, RollingWindow] | None = None,
    ) -> tuple[list[Order], PortfolioState]:
        """
        Run risk checks and return sized orders plus updated portfolio state.

        Returns empty orders when halted, no signal, or risk filter blocks trade.
        """
        updated_state = check_drawdown_breaker(
            account,
            portfolio_state,
            self.risk_manager.risk_config.max_drawdown_pct,
        )
        if updated_state.halted or signal is None:
            return [], updated_state

        orders = self.risk_manager.size_order(
            signal,
            window,
            account,
            positions,
            updated_state,
            risk_budget_fraction,
            strategy_params=strategy_params,
            asset_context=asset_context,
            active_windows=active_windows,
        )
        return orders, updated_state
