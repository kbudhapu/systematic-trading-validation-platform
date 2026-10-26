"""Shared mean-reversion backtest loop with time-stops and asymmetric entries."""

from __future__ import annotations

from src.backtest.metrics import max_drawdown, sharpe_ratio
from src.broker.simulated import SimulatedBroker
from src.config import AppConfig
from src.core.rolling_window import RollingWindow
from src.engine.leg_performance import match_position
from src.models import Bar, Order, PortfolioState, SignalAction
from src.router.default import OrderRouter
from src.strategies import get_strategy


def run_mean_reversion_backtest(
    bars_df,
    config: AppConfig,
    params: dict,
    *,
    allowed_long_dates: set[str] | None = None,
    allow_short: bool = True,
) -> dict:
    """Bar-by-bar backtest using the leg's strategy module."""
    strategy = get_strategy(config.strategy.module)
    broker = SimulatedBroker(slippage_pct=config.backtest.slippage_pct)
    router = OrderRouter(config.risk)
    state = PortfolioState(peak_equity=broker.equity)
    sma_period = int(params["sma_period"])
    window = RollingWindow(maxlen=sma_period + 50)
    equity_curve = [broker.equity]
    trade_pnls: list[float] = []
    prev_equity = broker.equity
    pending_orders: list[Order] | None = None
    bars_in_trade = 0
    symbol = config.strategy.symbol

    for row in bars_df.sort("timestamp").iter_rows(named=True):
        bar = Bar(
            timestamp=row["timestamp"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            symbol=row.get("symbol", symbol),
        )

        if pending_orders:
            fills = broker.submit_orders(pending_orders, fill_price=bar.open)
            if fills:
                new_eq = broker.get_account().equity
                trade_pnls.append(new_eq - prev_equity)
                prev_equity = new_eq
            pending_orders = None

        positions_before = broker.get_positions()
        in_position = match_position(symbol, positions_before) is not None
        if in_position:
            bars_in_trade += 1
        else:
            bars_in_trade = 0

        window.append(bar)
        if not window.is_ready(sma_period):
            broker._mark_equity(bar.close)
            equity_curve.append(broker.equity)
            continue

        if not state.halted:
            eval_params = {
                **params,
                "symbol": symbol,
                "bars_in_trade": bars_in_trade,
                "in_position": in_position,
            }
            signal = strategy.evaluate_live(window, eval_params)
            if signal and signal.action != SignalAction.HOLD:
                if (
                    allowed_long_dates is not None
                    and signal.action == SignalAction.LONG
                    and signal.timestamp.date().isoformat() not in allowed_long_dates
                ):
                    signal = None
                if not allow_short and signal and signal.action == SignalAction.SHORT:
                    signal = None

            if signal and signal.action != SignalAction.HOLD:
                account = broker.get_account()
                positions = broker.get_positions()
                orders, state = router.route(
                    signal, window, account, positions, state
                )
                if state.halted:
                    broker.close_all_positions(mark_price=bar.close)
                    prev_equity = broker.get_account().equity
                    bars_in_trade = 0
                elif orders:
                    pending_orders = orders

        broker._mark_equity(bar.close)
        equity_curve.append(broker.equity)

    total_return = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
    max_dd = max_drawdown(equity_curve)
    returns = [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
    sharpe = sharpe_ratio(returns)
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "total_trades": len(broker.fills),
        "profit_factor": profit_factor,
        "equity_curve": equity_curve,
    }
