"""Tests for control plane supervisor and config engine."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.control.config_watcher import ConfigWatcher
from src.engine.config_engine import (
    ConfigurationPrecedenceResolver,
    VaultBackupManager,
    assess_vault_integrity,
    run_human_bootstrap_playbook,
)
from src.engine.control_plane import (
    ControlPlaneSupervisor,
    CronLifecycleStage,
    RunbookStep,
    SupervisorState,
    dispatch_system_alert,
)
from src.persistence import db as persistence


def test_supervisor_runbook_success(tmp_path: Path) -> None:
    db_path = tmp_path / "research_vault.db"
    persistence.init_db()
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

    async def worker() -> dict:
        return {"vault_backup": "ok", "vault_backup_drill": {"verification_passed": True}}

    async def run() -> None:
        supervisor = ControlPlaneSupervisor(
            vault_path=db_path,
            workers={RunbookStep.POST_CLOSE_RECONCILIATION: worker},
            validators={
                RunbookStep.POST_CLOSE_RECONCILIATION: lambda payload: (True, ""),
            },
        )
        await supervisor.start()
        result = await supervisor.execute_runbook(RunbookStep.POST_CLOSE_RECONCILIATION)
        assert result.success is True
        assert result.stage == CronLifecycleStage.COMPLETED
        assert supervisor.state == SupervisorState.RUNNING

    asyncio.run(run())


def test_supervisor_runbook_validation_failure_alerts(tmp_path: Path) -> None:
    db_path = tmp_path / "research_vault.db"
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

    blocked: list[str] = []

    async def worker() -> dict:
        return {"vault_backup": None}

    async def run() -> None:
        supervisor = ControlPlaneSupervisor(
            vault_path=db_path,
            workers={RunbookStep.POST_CLOSE_RECONCILIATION: worker},
            validators={
                RunbookStep.POST_CLOSE_RECONCILIATION: lambda payload: (False, "backup missing"),
            },
            on_block_consumption=blocked.append,
        )
        await supervisor.start()
        with patch("src.engine.control_plane.dispatch_system_alert") as alert_mock:
            result = await supervisor.execute_runbook(RunbookStep.POST_CLOSE_RECONCILIATION)
        assert result.success is False
        assert supervisor.state == SupervisorState.DEGRADED
        assert blocked == ["backup missing"]
        alert_mock.assert_called_once()

    asyncio.run(run())


def test_dispatch_system_alert_log_fallback() -> None:
    with patch.dict("os.environ", {}, clear=True):
        result = asyncio.run(dispatch_system_alert("PRE_OPEN_HYDRATION", {"error": "test"}))
    assert result.delivered is False
    assert result.channel == "log_only"


def test_vault_backup_drill_checksum(tmp_path: Path) -> None:
    vault = tmp_path / "research_vault.db"
    with sqlite3.connect(vault) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('x')")

    manager = VaultBackupManager(
        vault_path=vault,
        staging_dir=tmp_path / "staging",
        mirror_dir=tmp_path / "mirror",
    )
    result = manager.execute_vault_backup_drill()
    assert result.verification_passed is True
    assert len(result.checksum_sha256) == 64
    assert Path(result.mirror_path).exists()


def test_configuration_precedence_resolver_order(tmp_path: Path) -> None:
    db_path = tmp_path / "research_vault.db"
    persistence.ensure_ai_policy_lifecycle_table(db_path)
    persistence.upsert_ai_policy_lifecycle_state(
        strategy_id="mean_reversion_qqq",
        symbol="QQQ",
        execution_state="PROBATIONAL_AI",
        db_path=db_path,
    )
    watcher = ConfigWatcher()
    watcher.set_runtime_params_override(
        "mean_reversion_qqq",
        {"max_position_pct": 0.42, "ai_policy_execution_state": "SOVEREIGN_AI"},
        source="test",
    )
    resolver = ConfigurationPrecedenceResolver(db_path=db_path)
    resolved = resolver.resolve_strategy_params(
        "mean_reversion_qqq",
        yaml_params={"max_position_pct": 0.95, "entry_z": 1.75},
        config_watcher=watcher,
        symbol="QQQ",
    )
    assert resolved.params["entry_z"] == 1.75
    assert resolved.params["ai_policy_execution_state"] == "SOVEREIGN_AI"
    assert resolved.params["max_position_pct"] == 0.42
    from src.engine.config_engine import ConfigLayer

    layers = [layer for layer, _ in resolved.precedence_trace]
    assert layers[0] == ConfigLayer.YAML_BASE
    assert layers[-1] == ConfigLayer.RUNTIME_OVERRIDE


def test_configuration_precedence_trace_layers(tmp_path: Path) -> None:
    db_path = tmp_path / "research_vault.db"
    resolver = ConfigurationPrecedenceResolver(db_path=db_path)
    resolved = resolver.resolve_strategy_params(
        "mean_reversion_qqq",
        yaml_params={"entry_z": 1.75},
    )
    from src.engine.config_engine import ConfigLayer

    assert resolved.precedence_trace[0][0] == ConfigLayer.YAML_BASE


def test_bootstrap_auto_seed_non_interactive(tmp_path: Path) -> None:
    db_path = tmp_path / "research_vault.db"
    result = run_human_bootstrap_playbook(
        vault_path=db_path,
        interactive=False,
        trust_auto_seed=True,
    )
    assert result.action == "auto_seed"
    assert result.auto_seed.get("seeded") is True
    integrity = assess_vault_integrity(db_path)
    assert integrity.champion_rows > 0
