"""
Walk-forward backtest engine.

Uses the same live code path (RollingWindow + evaluate_live + OrderRouter)
as production to guarantee backtest/live parity. Polars is used only to
load historical data from Parquet/API.

Execution model: signal on bar close → fill at next bar open (no look-ahead).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matplotlib.pyplot as plt
import structlog

from src.backtest.mean_reversion_loop import run_mean_reversion_backtest
from src.backtest.metrics import max_drawdown as _max_drawdown, sharpe_ratio as _sharpe
from src.config import AppConfig, ROOT
from src.ingestor.alpaca import AlpacaDataIngestor

log = structlog.get_logger()


@dataclass
class BacktestResult:
    """Summary metrics and pass/fail gates for a completed backtest run."""

    total_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    profit_factor: float
    equity_curve: list[float]
    passed: bool
    flags: list[str]


async def run_backtest(config: AppConfig) -> BacktestResult:
    """
    Run a walk-forward backtest using the live evaluation path.

    Signals are generated on bar close; fills occur at the following bar open.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.backtest.months * 30)

    ingestor = AlpacaDataIngestor(
        config.alpaca_api_key,
        config.alpaca_secret_key,
    )
    bars_df = await ingestor.fetch_historical(
        config.strategy.symbol,
        config.strategy.timeframe,
        start,
        end,
        use_cache=True,
    )

    if bars_df.is_empty():
        raise RuntimeError("No historical bars available for backtest")

    params = {**config.strategy.params, "symbol": config.strategy.symbol}
    result = run_backtest_on_bars(bars_df, config, params, save_chart=True)
    _print_summary(config.strategy.symbol, result)
    return result


def run_backtest_on_bars(
    bars_df,
    config: AppConfig,
    params: dict,
    *,
    save_chart: bool = False,
) -> BacktestResult:
    """
    Run backtest on pre-loaded bars with explicit strategy params.

    Used by parameter sweeps and walk-forward validation.
    """
    if bars_df.is_empty():
        raise RuntimeError("No bars provided for backtest")

    if config.strategy.module.startswith("mean_reversion_"):
        raw = run_mean_reversion_backtest(
            bars_df,
            config,
            params,
            allow_short=not config.risk.long_only,
        )
        equity_curve = raw.pop("equity_curve")
        flags = []
        if raw["sharpe"] < config.backtest.min_sharpe:
            flags.append(
                f"Sharpe {raw['sharpe']:.2f} below minimum {config.backtest.min_sharpe}"
            )
        if raw["max_drawdown"] > config.backtest.max_drawdown_pct:
            flags.append(
                f"Max drawdown {raw['max_drawdown']:.1%} exceeds limit "
                f"{config.backtest.max_drawdown_pct:.1%}"
            )
        result = BacktestResult(
            equity_curve=equity_curve,
            passed=len(flags) == 0,
            flags=flags,
            **raw,
        )
        if save_chart:
            _save_equity_chart(equity_curve)
        return result

    from src.broker.simulated import SimulatedBroker
    from src.core.rolling_window import RollingWindow
    from src.models import Bar, Order, PortfolioState, SignalAction
    from src.router.default import OrderRouter
    from src.strategies import get_strategy

    strategy = get_strategy(config.strategy.module)
    broker = SimulatedBroker(slippage_pct=config.backtest.slippage_pct)
    router = OrderRouter(config.risk)
    portfolio_state = PortfolioState(peak_equity=broker.equity)

    sma_period = params.get("sma_period", 20)
    window = RollingWindow(maxlen=sma_period + 50)

    equity_curve = [broker.equity]
    trade_pnls: list[float] = []
    prev_equity = broker.equity
    pending_orders: list[Order] | None = None

    sorted_bars = bars_df.sort("timestamp")
    for row in sorted_bars.iter_rows(named=True):
        bar = Bar(
            timestamp=row["timestamp"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            symbol=row.get("symbol", config.strategy.symbol),
        )

        if pending_orders:
            fills = broker.submit_orders(pending_orders, fill_price=bar.open)
            if fills:
                new_eq = broker.get_account().equity
                trade_pnls.append(new_eq - prev_equity)
                prev_equity = new_eq
            pending_orders = None

        window.append(bar)

        if not window.is_ready(sma_period):
            broker._mark_equity(bar.close)
            equity_curve.append(broker.equity)
            continue

        if not portfolio_state.halted:
            signal = strategy.evaluate_live(window, params)
            if signal and signal.action != SignalAction.HOLD:
                account = broker.get_account()
                positions = broker.get_positions()
                orders, portfolio_state = router.route(
                    signal, window, account, positions, portfolio_state
                )
                if portfolio_state.halted:
                    broker.close_all_positions(mark_price=bar.close)
                    prev_equity = broker.get_account().equity
                elif orders:
                    pending_orders = orders

        broker._mark_equity(bar.close)
        equity_curve.append(broker.equity)

    total_return = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
    max_dd = _max_drawdown(equity_curve)
    returns = [
        (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
    sharpe = _sharpe(returns)

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    flags = []
    if sharpe < config.backtest.min_sharpe:
        flags.append(f"Sharpe {sharpe:.2f} below minimum {config.backtest.min_sharpe}")
    if max_dd > config.backtest.max_drawdown_pct:
        flags.append(
            f"Max drawdown {max_dd:.1%} exceeds limit {config.backtest.max_drawdown_pct:.1%}"
        )

    result = BacktestResult(
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        total_trades=len(broker.fills),
        profit_factor=profit_factor,
        equity_curve=equity_curve,
        passed=len(flags) == 0,
        flags=flags,
    )

    if save_chart:
        _save_equity_chart(equity_curve)
    return result


def _save_equity_chart(equity: list[float]) -> None:
    """Persist equity curve PNG to project root."""
    plt.figure(figsize=(10, 4))
    plt.plot(equity)
    plt.title("Backtest Equity Curve")
    plt.xlabel("Bar")
    plt.ylabel("Equity ($)")
    plt.tight_layout()
    out = ROOT / "backtest_results.png"
    plt.savefig(out)
    plt.close()
    log.info("equity_chart_saved", path=str(out))


def backtest_result_to_dict(result) -> dict:
    """Serialize BacktestResult for API / Supabase storage."""
    return {
        "total_return": result.total_return,
        "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "total_trades": result.total_trades,
        "profit_factor": result.profit_factor,
        "equity_curve": result.equity_curve,
        "passed": result.passed,
        "flags": result.flags,
    }


def _print_summary(symbol: str, result: BacktestResult) -> None:
    """Print human-readable backtest summary to stdout."""
    print(f"\n{'='*50}")
    print(f"Backtest Results — {symbol}")
    print(f"{'='*50}")
    print(f"Total Return:    {result.total_return:>10.2%}")
    print(f"Sharpe Ratio:    {result.sharpe:>10.2f}")
    print(f"Max Drawdown:    {result.max_drawdown:>10.2%}")
    print(f"Win Rate:        {result.win_rate:>10.2%}")
    print(f"Total Trades:    {result.total_trades:>10d}")
    print(f"Profit Factor:   {result.profit_factor:>10.2f}")
    print(f"Passed Gates:    {'YES' if result.passed else 'NO'}")
    for flag in result.flags:
        print(f"  FLAG: {flag}")
    print(f"{'='*50}\n")
