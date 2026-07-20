"""Tests for adaptability chaos simulator, replay harness, and promotion dry-run."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.engine.challenger_registry import ChallengerRegistry
from src.engine.promotion_dry_run import PromotionDryRunEngine
from src.models import Bar
from src.persistence import db as persistence
from src.router.risk_manager import (
    AI_POLICY_PASSIVE_SHADOW,
    AI_POLICY_PROBATIONAL,
    AI_POLICY_SOVEREIGN,
)
from tests.harness.adaptability_harness import (
    AdaptabilityChaosSimulator,
    ChaosDrill,
    HistoricalStateReplayHarness,
    ReplayPolicyCheckpoint,
)


def _init_vault(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    persistence.ensure_regime_champions_table(db_path)
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    from src.engine.challenger_registry import ensure_challenger_schema

    ensure_challenger_schema(db_path)
    persistence.upsert_baseline_regime_champion(
        symbol="QQQ",
        params={"runtime_regime": "CALM_MR", "holdout_sharpe": 1.2},
        promoted_at=datetime.now(timezone.utc).isoformat(),
        db_path=db_path,
    )


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "research_vault.db"
    _init_vault(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS maintenance_job_ledger (
                job_id TEXT PRIMARY KEY,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_status TEXT NOT NULL,
                last_error TEXT,
                validation_passed INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT
            );
            """
        )
    persistence.upsert_maintenance_job_status(
        job_id="pre_open_readiness",
        status="success",
        validation_passed=True,
        db_path=db_path,
        success=True,
    )
    return db_path


def test_chaos_simulator_all_drills(vault_path: Path) -> None:
    simulator = AdaptabilityChaosSimulator(vault_path=vault_path)
    for drill in ChaosDrill:
        result = simulator.run_drill(drill)
        assert result.passed is True, f"{drill.value} failed: {result.observations}"
        assert len(result.assertions) >= 2


def test_historical_replay_rollback_at_expected_bar(vault_path: Path) -> None:
    base = datetime(2026, 6, 24, 14, 30, tzinfo=timezone.utc)
    bars = [
        Bar(
            timestamp=base + timedelta(minutes=15 * i),
            open=500.0,
            high=501.0,
            low=499.0,
            close=500.0 + i * 0.1,
            volume=100_000.0,
            symbol="QQQ",
        )
        for i in range(10)
    ]
    harness = HistoricalStateReplayHarness(vault_path=vault_path)
    result = harness.replay(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        bars=bars,
        policy_timeline=[
            ReplayPolicyCheckpoint(
                bar_index=3,
                execution_state=AI_POLICY_SOVEREIGN,
                params={},
                trigger_rollback=True,
                rollback_reason="sovereign_edge_decay",
                catastrophic=False,
                expected_post_state=AI_POLICY_PROBATIONAL,
                expected_tier="SOVEREIGN_TO_PROBATIONAL",
            ),
            ReplayPolicyCheckpoint(
                bar_index=7,
                execution_state=AI_POLICY_PROBATIONAL,
                params={},
                trigger_rollback=True,
                rollback_reason="probation_edge_decay",
                catastrophic=False,
                expected_post_state=AI_POLICY_PASSIVE_SHADOW,
                expected_tier="PROBATIONAL_TO_PASSIVE",
            ),
        ],
    )
    assert result.passed is True
    assert len(result.transitions) == 2
    assert result.transitions[0].bar_index == 3
    assert result.transitions[1].bar_index == 7


def test_promotion_dry_run_does_not_modify_production(vault_path: Path) -> None:
    registry = ChallengerRegistry(db_path=vault_path)
    registry.register_challenger(
        challenger_id="dry-run-challenger",
        champion_id="champion:GLOBAL:SHADOW_ML_OPTIMIZED_WEIGHTS",
        model_metadata={
            "strategy_id": "mean_reversion_qqq",
            "symbol": "QQQ",
        },
    )
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PASSIVE_SHADOW,
        db_path=vault_path,
    )

    engine = PromotionDryRunEngine(production_db_path=vault_path)
    try:
        result = engine.simulate_lifecycle_progression(
            "dry-run-challenger",
            force_catastrophic_rollback=True,
            rollback_reason="dry_run_eviction_test",
            live_hit_rate=0.15,
        )
        assert result.production_vault_unmodified is True
        assert result.final_state == AI_POLICY_PASSIVE_SHADOW
        assert result.rollback_checks is not None
        assert result.rollback_checks["tier"] == "CATASTROPHIC_EVICTION"

        stored = persistence.get_ai_policy_lifecycle_state(
            "mean_reversion_qqq",
            vault_path,
        )
        assert stored is not None
        assert stored["execution_state"] == AI_POLICY_PASSIVE_SHADOW
    finally:
        engine.close()


def test_promotion_dry_run_memory_isolated(vault_path: Path) -> None:
    registry = ChallengerRegistry(db_path=vault_path)
    registry.register_challenger(
        challenger_id="memory-challenger",
        champion_id="champion:GLOBAL:SHADOW_ML_OPTIMIZED_WEIGHTS",
        model_metadata={"strategy_id": "mean_reversion_qqq", "symbol": "QQQ"},
    )
    engine = PromotionDryRunEngine(production_db_path=vault_path)
    try:
        result = engine.simulate_lifecycle_progression(
            "memory-challenger",
            simulate_evening=False,
        )
        snapshot = engine.memory_snapshot()
        assert snapshot["initialized"] is True
        assert str(snapshot["vault_path"]).endswith("dry_run_vault.db")
        assert result.initial_state in {
            AI_POLICY_PASSIVE_SHADOW,
            AI_POLICY_PROBATIONAL,
            AI_POLICY_SOVEREIGN,
        }
    finally:
        engine.close()
