"""Tests for AI policy lifecycle sieve and challenger registry."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.engine.challenger_registry import (
    CHAMPION_DEFAULT_ID,
    ChallengerRegistry,
    ensure_challenger_schema,
)
from src.engine.policy_lifecycle import (
    AIPolicyLifecycleManager,
    STATE_PASSIVE,
    STATE_PROBATIONAL,
    STATE_SOVEREIGN,
)
from src.persistence import db as persistence
from src.router.risk_manager import AI_POLICY_PASSIVE_SHADOW


def _seed_shadow_ledger(db_path: Path, *, rows: int = 520) -> None:
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
        for i in range(rows):
            regime = "CALM_MR" if i % 2 == 0 else "HIGH_VOL_MR"
            rules_action = "DEFENSIVE_FALLBACK" if i % 3 == 0 else "CALM_MR"
            conn.execute(
                """
                INSERT INTO shadow_rl_ledger (
                    timestamp, symbol, state_vector, shadow_action_taken,
                    realized_reward_1h, realized_reward_24h, rules_engine_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"2026-05-{(i % 28) + 1:02d}T15:00:00+00:00",
                    "QQQ",
                    "[]",
                    "ALLOCATION_MAX",
                    0.02,
                    0.015,
                    rules_action,
                ),
            )


def _seed_stratified_attribution(db_path: Path) -> None:
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


def test_probation_entry_requires_dual_windows(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    _seed_shadow_ledger(db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    fail = manager.evaluate_probation_entry({"strategy_id": "mean_reversion_qqq", "symbol": "QQQ"})
    assert fail.approved is False

    _seed_stratified_attribution(db_path)
    ok = manager.evaluate_probation_entry({"strategy_id": "mean_reversion_qqq", "symbol": "QQQ"})
    assert ok.approved is True
    assert ok.window_30d is not None
    assert ok.window_60d is not None


def test_staged_rollback_sovereign_to_probational(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.execute_staged_rollback(
        STATE_SOVEREIGN,
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        reason="edge_decay",
        catastrophic=False,
    )
    assert result.new_state == STATE_PROBATIONAL
    row = persistence.get_ai_policy_lifecycle_state("mean_reversion_qqq", db_path)
    assert row is not None
    assert row["execution_state"] == STATE_PROBATIONAL


def test_evening_progression_passive_to_probational(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.ensure_regime_champions_table(db_path)
    _seed_shadow_ledger(db_path)
    _seed_stratified_attribution(db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.run_evening_progression(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})]
    )
    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == STATE_PROBATIONAL


def test_challenger_counterfactual_log(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    ensure_challenger_schema(db_path)
    persistence.ensure_regime_champions_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO regime_champions (
                symbol, regime, params_json, composite_score, promoted_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "GLOBAL",
                "SHADOW_ML_OPTIMIZED_WEIGHTS",
                json.dumps({"action": "ALLOCATION_MAX"}),
                2.5,
                datetime.now(timezone.utc).isoformat(),
                1,
            ),
        )
    registry = ChallengerRegistry(db_path=db_path)
    challenger = registry.ensure_default_challenger(CHAMPION_DEFAULT_ID)
    result = registry.log_counterfactual_state(
        CHAMPION_DEFAULT_ID,
        challenger.challenger_id,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": "QQQ",
            "rules_engine_action": "CALM_MR",
            "realized_reward_24h": 0.01,
            "state_vector": [0.1, 0.2, 0.0, 0.0, 0.5, 0.1],
            "regime_id": "CALM_MR",
        },
    )
    assert result.log_id > 0
    history = registry.fetch_counterfactual_history(
        champion_id=CHAMPION_DEFAULT_ID,
        challenger_id=challenger.challenger_id,
        limit=5,
    )
    assert len(history) == 1
