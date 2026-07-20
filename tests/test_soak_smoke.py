"""P4 soak integration smoke (ALL MOCK): the garage meeting itself before it meets
reality. Boots the soak pieces against mock data for a compressed synthetic session
and asserts each safety reflex fires. No live/paper order is submitted anywhere."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.config import CONFIG_DIR
from src.config.schema_check import SchemaValidationError, load_schema, validate_instance, validate_or_raise
from src.control.heartbeat_watchdog import HeartbeatWatchdog, LogNotifier
from src.data_quality.monitors import DataQualityConfig, DataQualityMonitors, DataQualityVerdict
from src.engine.engine_preemption import RiskEscalationEngine, RiskEscalationLevel
from src.execution.idempotent_execution import reconcile_boot
from src.models import Bar
from src.persistence.alert_store import read_alert_count
from src.persistence.heartbeat_store import read_latest_heartbeat, write_heartbeat_sync
from src.persistence.reconciliation_store import (
    persist_reconciliation_rows, read_fill_cost_count, read_timing_count,
)

RTH = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)   # Wed, mid-session ET


def _run(coro):
    return asyncio.run(coro)


def test_soak_integration_smoke(tmp_path: Path) -> None:
    print("\n=== SOAK INTEGRATION SMOKE (all mock) ===")
    db = str(tmp_path / "soak.db")
    esc = RiskEscalationEngine()

    # [1] heartbeat rows appear
    write_heartbeat_sync("main_loop", db, beat_utc=RTH - timedelta(seconds=5))
    assert read_latest_heartbeat("main_loop", db) is not None
    print("[1] heartbeat row written + read OK")

    # [2] planted stale feed -> data-quality blocks new entries on the instrument
    dq = DataQualityMonitors(DataQualityConfig(enabled=True))
    stale_bar = Bar(timestamp=RTH - timedelta(minutes=120), open=100, high=101, low=99,
                    close=100, volume=1e6, symbol="QQQ")
    res = _run(dq.evaluate(stale_bar, now=RTH, timeframe_minutes=15))
    assert res.verdict == DataQualityVerdict.BLOCK_NEW_ENTRIES and dq.entries_blocked("QQQ")
    print(f"[2] stale feed -> {res.verdict.value}, entries blocked on QQQ")

    # [3] planted reconcile mismatch -> ENTRY_GATE_HALT (block new entries)
    report = reconcile_boot({"QQQ": 10.0}, {"QQQ": 7.0}, escalation=esc)
    assert report.tripped and esc.blocks_all_entries()
    print(f"[3] reconcile mismatch -> tripped, blocks_all_entries={esc.blocks_all_entries()}")

    # [4] heartbeat watchdog + LogNotifier (a STALLED loop: its latest beat is old)
    wd_db = str(tmp_path / "soak_wd.db")
    write_heartbeat_sync("main_loop", wd_db, beat_utc=RTH - timedelta(seconds=600))
    esc2 = RiskEscalationEngine()
    wd = HeartbeatWatchdog(db_path=wd_db, escalation=esc2, notifier=LogNotifier(wd_db),
                           stale_threshold_seconds=120)
    wr = wd.check(now=RTH)
    assert wr.tripped and esc2.blocks_all_entries() and read_alert_count(wd_db, kind="heartbeat_stale") == 1
    print("[4] stale heartbeat -> watchdog tripped + LogNotifier alert row")

    # [5] kill switch flattens (reuse the drill's flatten path)
    from tests.test_kill_switch_drill import MockFlattenBroker, _Pos
    from src.engine.flatten_protocol import UnifiedFlattenProtocol
    from src.persistence.governance_state_store import PendingOrderStore
    esc.transition(RiskEscalationLevel.GLOBAL_FLATTEN_AND_HALT, commanded_by="operator")
    broker = MockFlattenBroker([_Pos("QQQ", 10, "long", 400.0)])
    proto = UnifiedFlattenProtocol(broker, PendingOrderStore(db_path=tmp_path / "t.db"))
    flat = _run(proto.execute_portfolio_flatten(reason="soak_kill_drill"))
    assert flat.executed and set(flat.symbols_flattened) == {"QQQ"}
    print("[5] kill switch -> portfolio flatten of QQQ (mock, no live order)")

    # [6] Stage-5 reconciliation tracker populates from day one (writer path)
    persist_reconciliation_rows([
        {"record_type": "fill_cost", "leg_id": "mean_reversion_qqq", "symbol": "QQQ",
         "side": "buy", "modeled_cost_bps": 5.0, "realized_cost_bps": 6.5},
        {"record_type": "timing_delta", "leg_id": "mean_reversion_qqq", "symbol": "QQQ",
         "latency_ms": 180.0},
    ], db)
    assert read_fill_cost_count(db) == 1 and read_timing_count(db) == 1
    print("[6] Stage-5 reconciliation tables populated (fill cost + timing delta)")

    # [7] restart resumes clean: matched reconcile -> no trip
    esc3 = RiskEscalationEngine()
    clean = reconcile_boot({"QQQ": 10.0}, {"QQQ": 10.0}, escalation=esc3)
    assert clean.clean and not esc3.blocks_all_entries()
    print("[7] restart reconcile clean -> entries not blocked")


def test_reconciliation_writer_dispatch(tmp_path: Path) -> None:
    from src.persistence.db_queue import AsyncDBWriter, QueuedWrite, WriteKind
    db = str(tmp_path / "recon.db")
    w = AsyncDBWriter(db_path=str(tmp_path / "wal.db"))
    w._postgres_settings = None
    items = [QueuedWrite(kind=WriteKind.RECONCILIATION,
                         payload={"record_type": "fill_cost", "leg_id": "qqq", "symbol": "QQQ",
                                  "modeled_cost_bps": 5.0, "realized_cost_bps": 5.5}, db_path=db)]
    assert w._flush_reconciliation_batch(items, Path(db)) is True
    assert read_fill_cost_count(db) == 1


# ---- soak config parity ----

def test_soak_block_matches_schema_and_coherent() -> None:
    """K5: the soak is LAUNCHED (soak.enabled may be true during the soak window,
    false once reverted). Assert schema validity + the invariants that hold in BOTH
    states, so this passes whether the soak is armed or reverted."""
    with (CONFIG_DIR / "env.yaml").open(encoding="utf-8") as fh:
        env = yaml.safe_load(fh)
    block = env["soak"]
    assert validate_instance(block, load_schema(CONFIG_DIR / "soak.schema.json")) == []
    assert isinstance(block["enabled"], bool)
    assert block["data_quality_enabled"] is True and block["reconcile_on_boot"] is True
    assert 0 < block["drawdown_breaker_test_pct"] < 0.10, "TEST threshold tighter than prod 0.10"
    # data-quality monitors are armed/reverted together with the soak (revert plan).
    assert env["data_quality"]["enabled"] == block["enabled"]
    # commissioning is paper-only, and non-empty only while the soak is armed.
    if block.get("commissioning_legs"):
        assert env["environment"] == "paper" and block["enabled"] is True


def test_malformed_soak_block_rejected() -> None:
    with (CONFIG_DIR / "env.yaml").open(encoding="utf-8") as fh:
        block = dict(yaml.safe_load(fh)["soak"])
    schema = load_schema(CONFIG_DIR / "soak.schema.json")
    with pytest.raises(SchemaValidationError):
        validate_or_raise({**block, "notifier": "email"}, schema)   # email not built
    with pytest.raises(SchemaValidationError):
        validate_or_raise({**block, "bogus": 1}, schema)


def test_parity_auditor_green_with_soak_block() -> None:
    from src.config import load_config
    from src.config.parity_auditor import ConfigurationParityAuditor
    ConfigurationParityAuditor().audit_app_config(load_config())
