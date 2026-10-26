"""
Shadow RL counterfactual metrics without the policy-brain training pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from src.persistence import db as persistence
from src.persistence.db import RESEARCH_VAULT_PATH

SHADOW_POLICY_LOOKBACK_SESSIONS = 30
SHADOW_ALLOCATION = {
    "STAND_DOWN": 0.0,
    "ALLOCATION_HALF": 0.5,
    "ALLOCATION_MAX": 1.0,
}
RULES_ALLOCATION = {
    "NO_TRADE_FALLBACK": 0.0,
    "DEFENSIVE_FALLBACK": 0.25,
    "BASELINE_FALLBACK": 0.5,
    "UNSAFE_DISABLED": 0.0,
    "BEAR_DEFENSIVE": 0.25,
    "HIGH_VOL_MR": 1.0,
    "CALM_MR": 1.0,
    "UNKNOWN": 0.75,
}


def rules_allocation(action: str) -> float:
    return float(RULES_ALLOCATION.get(str(action or "UNKNOWN"), 0.75))


def sharpe(series: np.ndarray) -> float:
    if series.size < 2:
        return 0.0
    mean = float(np.mean(series))
    std = float(np.std(series, ddof=1))
    if std <= 1e-12:
        return 0.0
    return mean / std * float(np.sqrt(series.size))


@dataclass(frozen=True)
class CounterfactualEquityResult:
    lookback_sessions: int
    sample_count: int
    cumulative_return: float
    return_variance: float
    sharpe_ratio: float
    rules_cumulative_return: float
    rules_sharpe_ratio: float
    equity_curve: list[float]
    period_returns: list[float]


class ShadowPolicyMetricsEngine:
    """Passive counterfactual evaluator for shadow RL ledger tuples."""

    def __init__(
        self,
        db_path: Path = RESEARCH_VAULT_PATH,
        lookback_sessions: int = SHADOW_POLICY_LOOKBACK_SESSIONS,
    ) -> None:
        self.db_path = db_path
        self.lookback_sessions = lookback_sessions

    def _load_ledger_window(self) -> pl.DataFrame:
        if not self.db_path.exists():
            return pl.DataFrame()
        persistence.init_db()
        with sqlite3.connect(self.db_path) as conn:
            df = pl.read_database(
                """
                SELECT
                    timestamp,
                    symbol,
                    shadow_action_taken,
                    realized_reward_24h,
                    rules_engine_action
                FROM shadow_rl_ledger
                WHERE realized_reward_24h IS NOT NULL
                ORDER BY timestamp
                """,
                conn,
            )
        if df.is_empty():
            return df
        session_dates = (
            df.with_columns(pl.col("timestamp").str.slice(0, 10).alias("session_date"))
            .select("session_date")
            .unique()
            .sort("session_date")
        )
        if session_dates.height > self.lookback_sessions:
            cutoff = session_dates.tail(self.lookback_sessions).row(0)[0]
            df = df.filter(pl.col("timestamp").str.slice(0, 10) >= cutoff)
        return df

    def compute_counterfactual_equity(
        self,
        df: pl.DataFrame | None = None,
    ) -> CounterfactualEquityResult:
        if df is None:
            df = self._load_ledger_window()

        period_returns: list[float] = []
        rules_returns: list[float] = []
        equity_curve = [1.0]

        for row in df.iter_rows(named=True):
            action_name = str(row.get("shadow_action_taken") or "")
            reward = row.get("realized_reward_24h")
            rules_action = str(row.get("rules_engine_action") or "UNKNOWN")
            if reward is None or action_name not in SHADOW_ALLOCATION:
                continue
            shadow_mult = float(SHADOW_ALLOCATION[action_name])
            rules_mult = rules_allocation(rules_action)
            period_return = shadow_mult * float(reward)
            rules_return = rules_mult * float(reward)
            period_returns.append(period_return)
            rules_returns.append(rules_return)
            equity_curve.append(equity_curve[-1] * (1.0 + period_return))

        if not period_returns:
            return CounterfactualEquityResult(
                lookback_sessions=self.lookback_sessions,
                sample_count=0,
                cumulative_return=0.0,
                return_variance=0.0,
                sharpe_ratio=0.0,
                rules_cumulative_return=0.0,
                rules_sharpe_ratio=0.0,
                equity_curve=[1.0],
                period_returns=[],
            )

        returns_arr = np.asarray(period_returns, dtype=np.float64)
        rules_arr = np.asarray(rules_returns, dtype=np.float64)
        cumulative = float(equity_curve[-1] - 1.0)
        rules_cumulative = float(np.prod(1.0 + rules_arr) - 1.0)
        return CounterfactualEquityResult(
            lookback_sessions=self.lookback_sessions,
            sample_count=len(period_returns),
            cumulative_return=cumulative,
            return_variance=float(np.var(returns_arr)),
            sharpe_ratio=sharpe(returns_arr),
            rules_cumulative_return=rules_cumulative,
            rules_sharpe_ratio=sharpe(rules_arr),
            equity_curve=equity_curve,
            period_returns=period_returns,
        )

    def persist_counterfactual(self, result: CounterfactualEquityResult) -> None:
        if result.sample_count <= 0:
            return
        persistence.log_shadow_policy_performance(
            lookback_sessions=result.lookback_sessions,
            sample_count=result.sample_count,
            cumulative_return=result.cumulative_return,
            return_variance=result.return_variance,
            sharpe_ratio=result.sharpe_ratio,
            rules_cumulative_return=result.rules_cumulative_return,
            rules_sharpe_ratio=result.rules_sharpe_ratio,
            equity_curve_json=json.dumps(result.equity_curve),
            db_path=self.db_path,
        )

    def run_evening_evaluation(self) -> dict[str, Any]:
        result = self.compute_counterfactual_equity()
        self.persist_counterfactual(result)
        return {
            "lookback_sessions": result.lookback_sessions,
            "sample_count": result.sample_count,
            "cumulative_return": result.cumulative_return,
            "return_variance": result.return_variance,
            "sharpe_ratio": result.sharpe_ratio,
            "rules_cumulative_return": result.rules_cumulative_return,
            "rules_sharpe_ratio": result.rules_sharpe_ratio,
        }
