"""
Parameter sweep for mean reversion — train/holdout validation.

Scores configs on in-sample data, validates top candidates out-of-sample
to reduce overfitting (standard desk practice).
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import polars as pl
import structlog

from src.backtest.engine import BacktestResult, run_backtest_on_bars
from src.config import AppConfig, load_config
from src.ingestor.alpaca import AlpacaDataIngestor

log = structlog.get_logger()


@dataclass
class SweepRow:
    """One parameter combination with train and holdout metrics."""

    sma_period: int
    threshold_sigma: float
    exit_sigma: float
    train: BacktestResult
    holdout: BacktestResult

    @property
    def train_score(self) -> float:
        """Desk-style composite: Sharpe penalized for drawdown and low activity."""
        if self.train.total_trades < 15:
            return -999.0
        if self.train.max_drawdown > 0.15:
            return -999.0
        pf = min(self.train.profit_factor, 3.0)
        return self.train.sharpe * 0.6 + pf * 0.2 - self.train.max_drawdown * 2.0


def _split_bars(bars_df: pl.DataFrame, train_frac: float = 0.7) -> tuple[pl.DataFrame, pl.DataFrame]:
    sorted_df = bars_df.sort("timestamp")
    n = len(sorted_df)
    cut = int(n * train_frac)
    return sorted_df.head(cut), sorted_df.tail(n - cut)


async def run_parameter_sweep(
    config: AppConfig | None = None,
    train_frac: float = 0.7,
    top_n: int = 10,
) -> list[SweepRow]:
    """Grid search mean reversion params with train/holdout split."""
    config = config or load_config()
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
    train_df, holdout_df = _split_bars(bars_df, train_frac)

    sma_grid = [10, 15, 20, 30, 50]
    threshold_grid = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
    exit_grid = [0.1, 0.2, 0.3, 0.5]

    rows: list[SweepRow] = []
    symbol = config.strategy.symbol

    for sma, thresh, exit_sig in itertools.product(sma_grid, threshold_grid, exit_grid):
        params = {
            "sma_period": sma,
            "threshold_sigma": thresh,
            "exit_sigma": exit_sig,
            "symbol": symbol,
        }
        train_res = run_backtest_on_bars(train_df, config, params)
        holdout_res = run_backtest_on_bars(holdout_df, config, params)
        rows.append(
            SweepRow(
                sma_period=sma,
                threshold_sigma=thresh,
                exit_sigma=exit_sig,
                train=train_res,
                holdout=holdout_res,
            )
        )

    rows.sort(key=lambda r: r.train_score, reverse=True)
    return rows[:top_n] if top_n else rows


def print_sweep_report(rows: list[SweepRow], baseline: SweepRow | None = None) -> None:
    """Print ranked sweep results."""
    print("\n" + "=" * 90)
    print("PARAMETER SWEEP — train (70%) / holdout (30%)")
    print("=" * 90)
    print(
        f"{'Rank':<5} {'SMA':<5} {'EntryS':<7} {'ExitS':<6} "
        f"{'TrSharpe':<9} {'TrRet':<8} {'TrDD':<7} {'Trades':<7} "
        f"{'HOSp':<8} {'HORet':<8} {'HODD':<7} {'HOTr':<6}"
    )
    print("-" * 90)
    for i, row in enumerate(rows, 1):
        t, h = row.train, row.holdout
        print(
            f"{i:<5} {row.sma_period:<5} {row.threshold_sigma:<7.2f} {row.exit_sigma:<6.2f} "
            f"{t.sharpe:<9.2f} {t.total_return:<8.2%} {t.max_drawdown:<7.1%} {t.total_trades:<7d} "
            f"{h.sharpe:<8.2f} {h.total_return:<8.2%} {h.max_drawdown:<7.1%} {h.total_trades:<6d}"
        )
    if baseline:
        t, h = baseline.train, baseline.holdout
        print("-" * 90)
        print(
            f"{'BASE':<5} {baseline.sma_period:<5} {baseline.threshold_sigma:<7.2f} "
            f"{baseline.exit_sigma:<6.2f} "
            f"{t.sharpe:<9.2f} {t.total_return:<8.2%} {t.max_drawdown:<7.1%} {t.total_trades:<7d} "
            f"{h.sharpe:<8.2f} {h.total_return:<8.2%} {h.max_drawdown:<7.1%} {h.total_trades:<6d}"
        )
    print("=" * 90 + "\n")


async def cmd_sweep() -> list[SweepRow]:
    """CLI entry: run full grid and print top 15."""
    from src.ssl_certs import install_ssl_certificates

    install_ssl_certificates()

    config = load_config()
    all_rows = await run_parameter_sweep(config, train_frac=0.7, top_n=0)
    all_rows.sort(key=lambda r: r.train_score, reverse=True)
    top = all_rows[:15]

    baseline_params = {**config.strategy.params, "symbol": config.strategy.symbol}
    baseline_row = next(
        (
            r
            for r in all_rows
            if r.sma_period == baseline_params.get("sma_period", 20)
            and r.threshold_sigma == baseline_params.get("threshold_sigma", 1.5)
            and r.exit_sigma == baseline_params.get("exit_sigma", 0.1)
        ),
        None,
    )

    print_sweep_report(top, baseline_row)

    if top:
        best = top[0]
        print("RECOMMENDED CONFIG (best train score, verify holdout):")
        print(
            f"  sma_period: {best.sma_period}\n"
            f"  threshold_sigma: {best.threshold_sigma}\n"
            f"  exit_sigma: {best.exit_sigma}\n"
            f"  Holdout Sharpe: {best.holdout.sharpe:.2f} | "
            f"Return: {best.holdout.total_return:.2%} | "
            f"DD: {best.holdout.max_drawdown:.1%} | "
            f"Trades: {best.holdout.total_trades}"
        )
    return top
