from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import polars as pl
import pytest

from src.engine.shadow_policy_metrics import ShadowPolicyMetricsEngine
from src.persistence import db as persistence


def _seed_ledger(db_path: Path) -> None:
    persistence.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
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
            );
            """
        )
        rows = [
            ("2026-06-01T15:00:00+00:00", "QQQ", "ALLOCATION_MAX", 0.02, "CALM_MR"),
            ("2026-06-02T15:00:00+00:00", "QQQ", "ALLOCATION_HALF", 0.04, "CALM_MR"),
            ("2026-06-03T15:00:00+00:00", "QQQ", "STAND_DOWN", 0.10, "CALM_MR"),
        ]
        for ts, symbol, action, reward, rules in rows:
            conn.execute(
                """
                INSERT INTO shadow_rl_ledger (
                    timestamp, symbol, state_vector, shadow_action_taken,
                    realized_reward_1h, realized_reward_24h, rules_engine_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, symbol, json.dumps([0.0] * 6), action, reward, reward, rules),
            )


def test_compute_counterfactual_equity(tmp_path: Path):
    db_path = tmp_path / "research_vault.db"
    _seed_ledger(db_path)
    engine = ShadowPolicyMetricsEngine(db_path=db_path, lookback_sessions=30)
    result = engine.compute_counterfactual_equity()
    assert result.sample_count == 3
    assert result.cumulative_return == pytest.approx(0.0404, rel=1e-3)
    assert result.sharpe_ratio != 0.0
    assert len(result.equity_curve) == 4


def test_persist_counterfactual_writes_table(tmp_path: Path):
    db_path = tmp_path / "research_vault.db"
    _seed_ledger(db_path)
    persistence.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
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
            );
            """
        )
    engine = ShadowPolicyMetricsEngine(db_path=db_path)
    summary = engine.run_evening_evaluation()
    assert summary["sample_count"] == 3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM shadow_policy_performance").fetchone()
    assert row is not None and int(row[0]) == 1
