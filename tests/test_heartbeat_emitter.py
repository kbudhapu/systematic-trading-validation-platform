"""R3 (INCIDENT-20260722): decoupled heartbeat emitter + status-aware watchdog + T5 resilient poll."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from src.control.heartbeat_emitter import (
    HeartbeatEmitter, SupabasePollMetrics, resilient_supabase_call,
)
from src.control.heartbeat_watchdog import HeartbeatWatchdog
from src.engine.engine_preemption import RiskEscalationEngine
from src.persistence.heartbeat_store import read_latest_heartbeat_status, write_heartbeat_status

RTH_NOW = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)


class _AlwaysOpen:
    def is_within_rth(self, now):
        return True


class _CapturingNotifier:
    def __init__(self):
        self.alerts = []

    def notify(self, alert):
        self.alerts.append(alert)


def _wd(db, esc, notif):
    wd = HeartbeatWatchdog(db_path=db, escalation=esc, notifier=notif, stale_threshold_seconds=120.0,
                           component="soak", started_at=RTH_NOW - timedelta(hours=1))
    wd._calendar = _AlwaysOpen()
    return wd


# --- emitter --------------------------------------------------------------------------------------
def test_emitter_writes_status_payload(tmp_path):
    db = str(tmp_path / "hb.db")
    lct = RTH_NOW - timedelta(seconds=5)
    emitter = HeartbeatEmitter(db_path=db, component="soak",
                               gate_state_fn=lambda: ("BLOCKED", "stale champions: QQQ"),
                               escalation_level_fn=lambda: "ENTRY_GATE_HALT",
                               last_cycle_ts_fn=lambda: lct)
    status = emitter.emit_once(beat_utc=RTH_NOW)
    assert status["gate"] == "BLOCKED" and status["gate_reason"] == "stale champions: QQQ"
    beat, read = read_latest_heartbeat_status("soak", db)
    assert read["gate"] == "BLOCKED" and read["escalation_level"] == "ENTRY_GATE_HALT"
    assert read["last_cycle_ts"] == lct.isoformat()


# --- watchdog: HALTED-BY-DESIGN vs DEAD vs DEAD-ENGINE ---------------------------------------------
def test_gate_blocked_is_warning_not_critical(tmp_path):
    """Fresh beat + current cycle + gate BLOCKED = halted-by-design -> WARNING, no escalation."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_status("soak", db, beat_utc=RTH_NOW - timedelta(seconds=5),
                           status={"gate": "BLOCKED", "gate_reason": "stale champions: QQQ",
                                   "escalation_level": "NOMINAL",
                                   "last_cycle_ts": (RTH_NOW - timedelta(seconds=10)).isoformat()})
    esc, notif = RiskEscalationEngine(), _CapturingNotifier()
    res = _wd(db, esc, notif).check(now=RTH_NOW)
    assert not res.tripped and res.reason == "gate_blocked"
    assert not esc.blocks_all_entries()                       # NOT escalated
    assert notif.alerts[0]["kind"] == "gate_blocked" and notif.alerts[0]["severity"] == "warning"
    assert "stale champions" in notif.alerts[0]["message"]


def test_dead_engine_with_live_emitter_trips_critical(tmp_path):
    """Fresh beat (emitter alive) but STALE last_cycle_ts (consumption dead) -> CRITICAL + escalate."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_status("soak", db, beat_utc=RTH_NOW - timedelta(seconds=5),
                           status={"gate": "OPEN", "gate_reason": "",
                                   "escalation_level": "NOMINAL",
                                   "last_cycle_ts": (RTH_NOW - timedelta(seconds=600)).isoformat()})
    esc, notif = RiskEscalationEngine(), _CapturingNotifier()
    res = _wd(db, esc, notif).check(now=RTH_NOW)
    assert res.tripped and res.reason == "engine_cycle_stale"
    assert esc.blocks_all_entries()                           # escalates -- a live HB over a dead engine
    assert notif.alerts[0]["kind"] == "heartbeat_stale" and notif.alerts[0]["severity"] == "critical"


def test_open_gate_and_fresh_cycle_is_healthy(tmp_path):
    db = str(tmp_path / "hb.db")
    write_heartbeat_status("soak", db, beat_utc=RTH_NOW - timedelta(seconds=5),
                           status={"gate": "OPEN", "gate_reason": "", "escalation_level": "NOMINAL",
                                   "last_cycle_ts": (RTH_NOW - timedelta(seconds=10)).isoformat()})
    esc, notif = RiskEscalationEngine(), _CapturingNotifier()
    res = _wd(db, esc, notif).check(now=RTH_NOW)
    assert not res.tripped and res.reason == "healthy" and notif.alerts == []


# --- T5 resilient poll ----------------------------------------------------------------------------
def test_resilient_call_success_and_metrics():
    async def scenario():
        m = SupabasePollMetrics()
        r = await resilient_supabase_call(lambda: 42, timeout_s=1.0, attempts=1, metrics=m)
        assert r == 42 and m.attempts == 1 and m.failures == 0
    asyncio.run(scenario())


def test_resilient_call_times_out_backs_off_and_counts():
    async def scenario():
        m = SupabasePollMetrics()
        r = await resilient_supabase_call(lambda: time.sleep(5), timeout_s=0.05, attempts=2,
                                          backoff_s=0.0, metrics=m)
        assert r is None and m.timeouts >= 1 and m.failures >= 1 and m.attempts == 2
    asyncio.run(scenario())


def test_emitter_owns_no_supabase_io(tmp_path):
    """A hanging Supabase poll must never delay a heartbeat: the emitter only calls its injected state
    fns + a local SQLite write. Proof: emit_once completes fast while a poll would hang for seconds."""
    db = str(tmp_path / "hb.db")
    emitter = HeartbeatEmitter(db_path=db, component="soak", gate_state_fn=lambda: ("OPEN", ""),
                               escalation_level_fn=lambda: "NOMINAL",
                               last_cycle_ts_fn=lambda: RTH_NOW)
    t0 = time.monotonic()
    emitter.emit_once()
    assert time.monotonic() - t0 < 1.0                        # instant -- no network in the emit path
    assert read_latest_heartbeat_status("soak", db) is not None
