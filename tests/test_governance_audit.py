"""Tests for governance journal, kill switches, and audit panel."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.engine.audit_panel import (
    DASHBOARD_SUMMARY_BAR_INTERVAL,
    GovernanceTelemetryProvider,
    generate_rollback_post_mortem,
)
from src.engine.governance import (
    EVENT_RECOVERY_ROLLBACK,
    ImmutableChangeJournal,
    KillLevel,
    HumanOverrideRegistry,
    TriggeredBy,
    ensure_governance_schema,
    verify_journal_signature,
)
from src.persistence import db as persistence
from src.router.risk_manager import AI_POLICY_PASSIVE_SHADOW


def test_journal_append_only_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    journal = ImmutableChangeJournal(db_path=db_path)
    entry = journal.append(
        event_type=EVENT_RECOVERY_ROLLBACK,
        triggered_by=TriggeredBy.SYSTEM_AUTOMATIC,
        previous_state={"x": 1},
        requested_state={"x": 2},
        rationale="unit test",
    )
    assert verify_journal_signature(entry)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE immutable_change_journal SET event_type = 'TAMPER' WHERE journal_id = ?",
                (entry.journal_id,),
            )


def test_kill_switches_isolated(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    state_file = tmp_path / "circuit_breaker_state.json"
    registry = HumanOverrideRegistry(db_path=db_path, state_file=state_file)
    registry.engage(
        KillLevel.STRATEGY_HALT,
        scope_key="mean_reversion_qqq",
        operator="ops",
        rationale="single leg review",
    )
    registry.engage(
        KillLevel.RESEARCH_HALT,
        scope_key="GLOBAL",
        operator="ops",
        rationale="pause sweeps",
    )
    snap = registry.snapshot()
    assert registry.is_strategy_halted("mean_reversion_qqq")
    assert not registry.is_portfolio_halted()
    assert registry.is_research_halted()
    assert not registry.is_ai_halted()
    assert len(snap.states) == 2
    assert state_file.exists()


def test_ai_halt_forces_passive_mode(tmp_path: Path) -> None:
    registry = HumanOverrideRegistry(
        db_path=tmp_path / "vault.db",
        state_file=tmp_path / "circuit.json",
    )
    registry.engage(
        KillLevel.AI_HALT,
        operator="risk",
        rationale="strip live ai authority",
    )
    params = registry.apply_ai_halt_to_params(
        {"ai_policy_execution_state": "SOVEREIGN_AI", "_shadow_action_live": "ALLOCATION_MAX"}
    )
    assert params["ai_policy_execution_state"] == AI_POLICY_PASSIVE_SHADOW
    assert "_shadow_action_live" not in params


def test_note_bar_closed_is_non_blocking(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    provider = GovernanceTelemetryProvider(db_path=db_path)
    provider.start_dashboard_aggregator()
    with patch.object(provider, "refresh_dashboard_summary") as refresh:
        for _ in range(DASHBOARD_SUMMARY_BAR_INTERVAL):
            provider.note_bar_closed()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not refresh.called:
            time.sleep(0.05)
    provider.stop_dashboard_aggregator()
    assert refresh.called


def test_operational_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state="PROBATIONAL_AI",
        probation_clean_trading_days=4,
        db_path=db_path,
    )
    persistence.ensure_regime_champions_table(db_path)
    persistence.upsert_baseline_regime_champion(
        symbol="QQQ",
        params={"entry_z": 1.75},
        promoted_at="2026-06-01T00:00:00+00:00",
        db_path=db_path,
    )
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
    provider = GovernanceTelemetryProvider(db_path=db_path)
    provider.refresh_dashboard_summary()
    snap = provider.fetch_operational_snapshot()
    assert "mean_reversion_qqq" in snap.ai_policy_states
    assert snap.ai_policy_states["mean_reversion_qqq"]["probation_clean_trading_days"] == 4
    assert "QQQ" in snap.champion_ages
    assert snap.maintenance_jobs["pre_open_readiness"]["status"] == "success"


def test_generate_rollback_post_mortem_markdown(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    journal = ImmutableChangeJournal(db_path=db_path)
    entry = journal.record_recovery_rollback(
        scope_key="mean_reversion_qqq",
        previous_state={"execution_state": "SOVEREIGN_AI"},
        requested_state={"execution_state": "PASSIVE_SHADOW"},
        rationale="live_hit_rate_floor",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS active_promotion_attribution (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_pnl REAL NOT NULL,
                bars_held INTEGER NOT NULL,
                slippage_pct REAL NOT NULL,
                execution_tax REAL NOT NULL,
                live_sharpe_10t REAL,
                holding_drift_ratio REAL,
                config_version_id INTEGER,
                params_json TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO active_promotion_attribution (
                timestamp, strategy_id, symbol, trade_pnl, bars_held,
                slippage_pct, execution_tax, live_sharpe_10t,
                holding_drift_ratio, config_version_id, params_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.timestamp,
                "mean_reversion_qqq",
                "QQQ",
                -12.5,
                3,
                0.002,
                0.002,
                -0.5,
                1.2,
                1,
                "{}",
            ),
        )
    report = generate_rollback_post_mortem(
        f"journal:{entry.journal_id}",
        db_path=db_path,
    )
    assert "# Rollback Post-Mortem" in report
    assert "Primary Root Cause" in report
    assert "Signal Failure" in report or "Implementation Shortfall" in report
