from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.policy_lifecycle import AIPolicyLifecycleManager
from src.persistence import db as persistence
from src.router.risk_manager import AI_POLICY_PASSIVE_SHADOW, AI_POLICY_PROBATIONAL


def _seed_stratified_attribution_for_test(db_path: Path) -> None:
    from src.engine.attribution import ensure_live_attribution_schema

    ensure_live_attribution_schema(db_path)
    regimes = ("CALM_MR", "HIGH_VOL_MR")
    sessions = ("OPENING_CROSS", "MIDDAY_DOLDRUMS", "CLOSING_IMBALANCE")
    with sqlite3.connect(db_path) as conn:
        idx = 0
        for day in range(65):
            for regime in regimes:
                for session in sessions:
                    for _ in range(2):
                        idx += 1
                        pnl = 4.0 + float(idx % 5)
                        conn.execute(
                            """
                            INSERT INTO live_attribution_ledger (
                                trade_id, timestamp, strategy_id, symbol, side, qty, pnl,
                                regime_id, session_type, liquidity_state, execution_tactic,
                                champion_version_id, ai_policy_execution_state, promotion_id,
                                expected_price, filled_price, slippage_pct, markout_5bar,
                                participation_cap_pct, metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"t-{idx}",
                                (datetime.now(timezone.utc) - timedelta(days=day)).isoformat(),
                                "mean_reversion_qqq",
                                "QQQ",
                                "sell",
                                1.0,
                                pnl,
                                regime,
                                session,
                                "NORMAL",
                                "PASSIVE",
                                1,
                                "PASSIVE_SHADOW",
                                None,
                                500.0,
                                501.0,
                                0.0005,
                                None,
                                0.95,
                                "{}",
                            ),
                        )


def _seed_performance(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_policy_performance (
                eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at TEXT NOT NULL,
                lookback_sessions INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                cumulative_return REAL NOT NULL,
                return_variance REAL NOT NULL,
                sharpe_ratio REAL NOT NULL,
                rules_cumulative_return REAL,
                rules_sharpe_ratio REAL,
                equity_curve_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO shadow_policy_performance (
                evaluated_at, lookback_sessions, sample_count,
                cumulative_return, return_variance, sharpe_ratio,
                rules_cumulative_return, rules_sharpe_ratio, equity_curve_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-24T00:00:00+00:00",
                30,
                600,
                0.08,
                0.001,
                1.5,
                0.04,
                1.0,
                "[1.0,1.08]",
            ),
        )


def test_policy_lifecycle_promotes_passive_to_probational(tmp_path: Path):
    db_path = tmp_path / "research_vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.ensure_regime_champions_table(db_path)
    _seed_performance(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_rl_ledger (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                state_vector JSON NOT NULL,
                shadow_action_taken TEXT NOT NULL,
                realized_reward_1h REAL,
                realized_reward_24h REAL,
                rules_engine_action TEXT NOT NULL,
                raw_policy_outputs TEXT
            )
            """
        )
        for i in range(500):
            conn.execute(
                """
                INSERT INTO shadow_rl_ledger (
                    timestamp, symbol, state_vector, shadow_action_taken,
                    realized_reward_1h, realized_reward_24h, rules_engine_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"2026-05-{(i % 28) + 1:02d}T15:00:{i%60:02d}+00:00",
                    "QQQ",
                    "[]",
                    "ALLOCATION_MAX",
                    0.02,
                    0.015,
                    "DEFENSIVE_FALLBACK" if i % 2 == 0 else "CALM_MR",
                ),
            )
    _seed_stratified_attribution_for_test(db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.run_evening_progression(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})]
    )
    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == AI_POLICY_PROBATIONAL
    row = persistence.get_ai_policy_lifecycle_state("mean_reversion_qqq", db_path)
    assert row is not None
    assert row["execution_state"] == AI_POLICY_PROBATIONAL
