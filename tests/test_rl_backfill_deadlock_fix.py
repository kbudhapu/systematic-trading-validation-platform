"""RL-reward backfill lag and the live-trading SLO.

Half A (SUPERSEDED by S2, 2026-07-13): the original "deadlock fix" capped rl_backfill_lag at SOFT.
That was a half-measure — a research/ML metric (the shadow RL reward backfill; the shadow policy does
not trade) has no place in the live-trading SLO at ALL. S2 REMOVES it from the live verdict entirely;
it is monitored + alerted on evaluate_research_pipeline_health and NEVER gates the order path. These
tests now assert that removal.
Half B: the delayed RL-reward backfill runs on a PARKED cycle (no due legs) and drains backfillable
        NULL rewards -- it no longer sits behind the enabled+due-legs gate that starved it.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.engine.degradation_manager import DegradationManager, OperationalMode
from src.engine.orchestrator import TradingOrchestrator
from src.engine.slo_monitor import (
    IntegritySeverity, SLOMonitor, query_rl_backfill_lag_hours,
)
from src.persistence.db import backfill_delayed_rl_rewards

_LEDGER_DDL = """
CREATE TABLE shadow_rl_ledger (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
  state_vector JSON NOT NULL, shadow_action_taken TEXT NOT NULL,
  realized_reward_1h REAL, realized_reward_24h REAL,
  rules_engine_action TEXT NOT NULL, raw_policy_outputs TEXT
);
"""


def _bt(hour, minute):
    return datetime(2026, 6, 24, hour, minute, tzinfo=timezone.utc)


def _payload(**over):
    """The fresh-bars baseline (severity OK), with per-field overrides. Freshness is measured from
    bar END, so a just-opened bar reads 0s -> OK baseline; overriding one field isolates its effect."""
    now = _bt(15, 0) + timedelta(minutes=5)   # 15:00 UTC = 11:00 ET, within RTH (T2e: stocks dormant off-RTH)
    latest = _bt(15, 0)
    p = {
        "exchange_reference_ts": now, "asset_class": "stock", "timeframe": "15Min",
        "latest_bar_timestamp": latest,
        "bar_timestamps": [latest - timedelta(minutes=15 * i) for i in range(6)],
        "nbbo_success_rate": 0.95, "rl_backfill_lag_hours": 2.0,
    }
    p.update(over)
    return p


# --------------------------------------------------------------------------- #
# HALF A — severity remap.
# --------------------------------------------------------------------------- #
def test_rl_backfill_lag_is_absent_from_the_live_verdict():
    # S2: rl_backfill_lag is a RESEARCH metric and no longer appears in the live SLO verdict at all.
    v = SLOMonitor().evaluate_data_integrity(_payload(rl_backfill_lag_hours=40.0))  # 40h stale
    assert v.severity == IntegritySeverity.OK                 # was SOFT (half-measure); now OUT entirely
    assert "rl_backfill_lag" not in v.reasons
    assert "rl_backfill_lag_critical" not in v.reasons
    assert v.rl_backfill_lag_hours == 40.0                    # still measured + carried for reporting


def test_live_feed_critical_still_hard_and_rl_contributes_nothing():
    # a genuine live-feed critical (nbbo below the hard floor) still maps to HARD
    assert SLOMonitor().evaluate_data_integrity(
        _payload(nbbo_success_rate=0.40)).severity == IntegritySeverity.HARD_BREACH
    # ... and rl_backfill contributes NO reason either way
    v = SLOMonitor().evaluate_data_integrity(_payload(nbbo_success_rate=0.40, rl_backfill_lag_hours=40.0))
    assert v.severity == IntegritySeverity.HARD_BREACH
    assert "nbbo_fetch_critical" in v.reasons
    assert "rl_backfill_lag" not in v.reasons and "rl_backfill_lag_critical" not in v.reasons


def test_rl_backfill_alone_does_not_degrade_at_all():
    # S2: rl_backfill lag alone yields an OK verdict -> NO degradation (not even SOFT).
    v_rl = SLOMonitor().evaluate_data_integrity(_payload(rl_backfill_lag_hours=40.0))
    assert DegradationManager().evaluate_from_slo(v_rl).mode == OperationalMode.NORMAL
    # a live-feed HARD verdict still latches HARD_CRITICAL_DEGRADE (control)
    v_hard = SLOMonitor().evaluate_data_integrity(_payload(nbbo_success_rate=0.40))
    assert DegradationManager().evaluate_from_slo(v_hard).mode == OperationalMode.HARD_CRITICAL_DEGRADE


# --------------------------------------------------------------------------- #
# HALF B — backfill drains when parked; wiring runs it on the parked cycle.
# --------------------------------------------------------------------------- #
def _ledger(path, ages_hours):
    with sqlite3.connect(path) as c:
        c.executescript(_LEDGER_DDL)
        now = datetime.now(timezone.utc)
        for age in ages_hours:
            c.execute(
                "INSERT INTO shadow_rl_ledger (timestamp,symbol,state_vector,shadow_action_taken,"
                "realized_reward_1h,realized_reward_24h,rules_engine_action) VALUES (?,?,?,?,?,?,?)",
                ((now - timedelta(hours=age)).isoformat(), "QQQ", "{}", "HOLD", None, None, "HOLD"))
    return path


def test_backfill_drains_backfillable_and_skips_out_of_window(tmp_path):
    db = _ledger(tmp_path / "v.db", [10, 48, 80])          # <24h, in 24-72h window, >72h
    updated = backfill_delayed_rl_rewards(lambda s, o, h: 0.05, db_path=db)
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT realized_reward_1h, realized_reward_24h FROM shadow_rl_ledger "
                         "ORDER BY timestamp DESC").fetchall()
    r10, r48, r80 = rows                                    # newest -> oldest
    assert r10 == (0.05, None)                              # 10h: 1h filled, 24h too young
    assert r48 == (0.05, 0.05)                              # 48h: both filled
    assert r80 == (None, None)                              # 80h: past MAX_AGE -> skipped
    assert updated == 2


def test_backfill_clears_lag_when_rows_in_window(tmp_path):
    db = _ledger(tmp_path / "v2.db", [30, 50, 70])          # all backfillable
    ref = datetime.now(timezone.utc)
    assert query_rl_backfill_lag_hours(db, reference_ts=ref) > 30.0   # deadlocked before
    backfill_delayed_rl_rewards(lambda s, o, h: 0.05, db_path=db)
    assert query_rl_backfill_lag_hours(db, reference_ts=ref) == 0.0   # drained -> no lag


def _run_lightweight(run_flag: bool):
    stub = MagicMock()
    stub.config.environment = "paper"
    stub._portfolio_uuid.return_value = "pid"
    for m in ("_check_wal_backlog_alert", "_check_sustained_degraded_feed_alert",
              "_advance_fill_reconciliation_sieve", "_maybe_backfill_rl_rewards",
              "_write_soak_heartbeat"):
        setattr(stub, m, AsyncMock())
    with patch("src.engine.orchestrator.persistence"):
        asyncio.run(TradingOrchestrator._lightweight_cycle_pass(stub, reason="t", run_rl_backfill=run_flag))
    return stub


def test_parked_cycle_runs_backfill():
    _run_lightweight(True)._maybe_backfill_rl_rewards.assert_awaited_once()


def test_shutdown_cycle_skips_backfill():
    _run_lightweight(False)._maybe_backfill_rl_rewards.assert_not_awaited()


def test_parked_cycle_writes_soak_heartbeat():
    """F2 (M3-RED-2): the soak liveness beat MUST be written on the lightweight (~60s) pass, not
    only on the full leg-due cycle (~15min). Otherwise the soak watchdog (120s threshold) would
    false-trip + ENTRY_GATE_HALT for ~13 of every 15 minutes during RTH."""
    _run_lightweight(True)._write_soak_heartbeat.assert_awaited_once()
    _run_lightweight(False)._write_soak_heartbeat.assert_awaited_once()   # even on the shutdown pass
