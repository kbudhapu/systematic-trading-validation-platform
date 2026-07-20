"""Tests for intraday velocity shock cool-off recovery."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.engine.challenger_registry import ensure_challenger_schema
from src.engine.governance import (
    EVENT_AI_LIFECYCLE_ADJUSTMENT,
    ImmutableChangeJournal,
    ensure_governance_schema,
)
from src.engine.policy_lifecycle import (
    AIPolicyLifecycleManager,
    VELOCITY_SHOCK_RECOVERY_REASON,
)
from src.engine.regime_intelligence import RegimeStabilizationVerdict
from src.persistence import db as persistence
from src.router.risk_manager import AI_POLICY_PASSIVE_SHADOW, AI_POLICY_SOVEREIGN


def _stabilization_verdict(*, stabilized: bool, reason: str) -> RegimeStabilizationVerdict:
    return RegimeStabilizationVerdict(
        stabilized=stabilized,
        stable_sub_sigma_bars=40 if stabilized else 10,
        required_bars=40,
        composite_stress_z_score=1.0 if stabilized else 2.0,
        seconds_since_last_shock=3600.0,
        reason=reason,
        recovery_sigma_threshold=1.5,
        shock_event_count_7d=1,
        shock_demotion_count_7d=1,
    )


def _seed_dual_policy_shadow_log(
    db_path: Path,
    *,
    symbol: str = "QQQ",
    rows: int = 8,
    challenger_pnl: float = 0.04,
    rules_pnl: float = 0.01,
    demoted_at: str,
) -> None:
    ensure_challenger_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        for i in range(rows):
            conn.execute(
                """
                INSERT INTO dual_policy_shadow_log (
                    timestamp, champion_id, challenger_id, symbol, session_type,
                    regime_id, market_state_json, champion_action, challenger_action,
                    champion_capital, challenger_capital, champion_pnl, challenger_pnl,
                    rules_baseline_pnl, matched_capital_notional, execution_path_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"2026-06-24T16:{i:02d}:00+00:00",
                    "champion:GLOBAL:SHADOW_ML_OPTIMIZED_WEIGHTS",
                    "challenger:default",
                    symbol,
                    "MIDDAY_DOLDRUMS",
                    "CALM_MR",
                    "{}",
                    "ALLOCATION_MAX",
                    "ALLOCATION_MAX",
                    1.0,
                    1.0,
                    challenger_pnl,
                    challenger_pnl,
                    rules_pnl,
                    1000.0,
                    json.dumps({"path": "shadow"}),
                ),
            )


def test_intraday_recovery_restores_pre_shock_tier(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    ensure_governance_schema(db_path)
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    demoted_at = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc).isoformat()
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PASSIVE_SHADOW,
        velocity_shock_prior_state=AI_POLICY_SOVEREIGN,
        velocity_shock_demoted_at=demoted_at,
        db_path=db_path,
    )
    _seed_dual_policy_shadow_log(db_path, demoted_at=demoted_at)

    journal = ImmutableChangeJournal(db_path=db_path)
    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.process_intraday_velocity_shock_recovery(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})],
        stabilization=_stabilization_verdict(stabilized=True, reason="regime_stabilized"),
        change_journal=journal,
    )

    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == AI_POLICY_SOVEREIGN
    assert transition["reason"] == VELOCITY_SHOCK_RECOVERY_REASON

    stored = persistence.get_ai_policy_lifecycle_state("mean_reversion_qqq", db_path)
    assert stored is not None
    assert stored["execution_state"] == AI_POLICY_SOVEREIGN
    assert stored.get("velocity_shock_prior_state") is None
    assert stored.get("velocity_shock_demoted_at") is None

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT event_type, rationale_hash
            FROM immutable_change_journal
            WHERE event_type = ?
            ORDER BY journal_id DESC
            LIMIT 1
            """,
            (EVENT_AI_LIFECYCLE_ADJUSTMENT,),
        ).fetchone()
    assert row is not None
    assert row[0] == EVENT_AI_LIFECYCLE_ADJUSTMENT


def test_intraday_recovery_blocks_until_stabilized(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    demoted_at = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc).isoformat()
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PASSIVE_SHADOW,
        velocity_shock_prior_state=AI_POLICY_SOVEREIGN,
        velocity_shock_demoted_at=demoted_at,
        db_path=db_path,
    )
    _seed_dual_policy_shadow_log(db_path, demoted_at=demoted_at)

    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.process_intraday_velocity_shock_recovery(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})],
        stabilization=_stabilization_verdict(
            stabilized=False,
            reason="cool_off_window_incomplete",
        ),
    )
    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == AI_POLICY_PASSIVE_SHADOW
    assert transition["reason"] == "velocity_shock_cool_off_pending"


def test_intraday_recovery_blocks_on_hysteresis_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    demoted_at = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc).isoformat()
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PASSIVE_SHADOW,
        velocity_shock_prior_state=AI_POLICY_SOVEREIGN,
        velocity_shock_demoted_at=demoted_at,
        db_path=db_path,
    )
    _seed_dual_policy_shadow_log(db_path, demoted_at=demoted_at)

    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.process_intraday_velocity_shock_recovery(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})],
        stabilization=_stabilization_verdict(
            stabilized=False,
            reason="composite_stress_above_hysteresis_threshold",
        ),
    )
    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == AI_POLICY_PASSIVE_SHADOW
    assert transition["reason"] == "velocity_shock_hysteresis_blocked"


def test_intraday_recovery_blocks_when_shadow_edge_lost(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    demoted_at = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc).isoformat()
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state=AI_POLICY_PASSIVE_SHADOW,
        velocity_shock_prior_state=AI_POLICY_SOVEREIGN,
        velocity_shock_demoted_at=demoted_at,
        db_path=db_path,
    )
    _seed_dual_policy_shadow_log(
        db_path,
        demoted_at=demoted_at,
        challenger_pnl=0.005,
        rules_pnl=0.05,
    )

    manager = AIPolicyLifecycleManager(db_path=db_path)
    result = manager.process_intraday_velocity_shock_recovery(
        [("mean_reversion_qqq", "QQQ", {"ai_policy_execution_state": AI_POLICY_PASSIVE_SHADOW})],
        stabilization=_stabilization_verdict(stabilized=True, reason="regime_stabilized"),
    )
    transition = result["transitions"]["mean_reversion_qqq"]
    assert transition["to"] == AI_POLICY_PASSIVE_SHADOW
    assert "shadow_challenger_underperforms_rules" in transition["reason"]
