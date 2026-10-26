"""G1.5 heartbeat watchdog: a stalled main loop trips block-new-entries + fires a
LogNotifier alert, but only while the market is open (no false alarms when closed)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.control.heartbeat_watchdog import HeartbeatWatchdog, LogNotifier, Notifier
from src.engine.engine_preemption import RiskEscalationEngine
from src.persistence.alert_store import read_alert_count
from src.persistence.heartbeat_store import write_heartbeat_sync

# Wed 2024-07-10 ~1pm ET (RTH open) and Sun 2024-07-14 (market closed).
RTH_NOW = datetime(2024, 7, 10, 17, 0, tzinfo=timezone.utc)
CLOSED_NOW = datetime(2024, 7, 14, 17, 0, tzinfo=timezone.utc)


class _CapturingNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict] = []

    def notify(self, alert: dict) -> None:
        self.alerts.append(alert)


def _watchdog(db, esc, notifier, threshold=120.0) -> HeartbeatWatchdog:
    return HeartbeatWatchdog(db_path=db, escalation=esc, notifier=notifier,
                             stale_threshold_seconds=threshold)


def test_fresh_heartbeat_healthy_no_trip(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("main_loop", db, beat_utc=RTH_NOW - timedelta(seconds=10))
    esc = RiskEscalationEngine()
    notifier = _CapturingNotifier()
    res = _watchdog(db, esc, notifier).check(now=RTH_NOW)
    assert not res.tripped and res.reason == "healthy"
    assert not esc.blocks_all_entries()
    assert notifier.alerts == []


def test_stale_heartbeat_trips_block_new_entries_and_notifies(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("main_loop", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    esc = RiskEscalationEngine()
    notifier = _CapturingNotifier()
    res = _watchdog(db, esc, notifier).check(now=RTH_NOW)
    assert res.tripped and res.reason == "stale_heartbeat" and res.age_seconds >= 600
    assert esc.blocks_all_entries(), "stale loop must force block-new-entries (safe_mode)"
    assert notifier.alerts and notifier.alerts[0]["kind"] == "heartbeat_stale"


def test_missing_heartbeat_trips(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    esc = RiskEscalationEngine()
    notifier = _CapturingNotifier()
    res = _watchdog(db, esc, notifier).check(now=RTH_NOW)   # no heartbeat ever written
    assert res.tripped and esc.blocks_all_entries()


def test_closed_market_does_not_trip(tmp_path: Path) -> None:
    """Market-calendar aware: a stale heartbeat while the market is CLOSED must not
    trip (the loop is idle by design overnight/weekends)."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("main_loop", db, beat_utc=CLOSED_NOW - timedelta(hours=10))
    esc = RiskEscalationEngine()
    notifier = _CapturingNotifier()
    res = _watchdog(db, esc, notifier).check(now=CLOSED_NOW)
    assert not res.tripped and res.reason == "market_closed"
    assert not esc.blocks_all_entries()
    assert notifier.alerts == []


def test_log_notifier_writes_alert_row(tmp_path: Path) -> None:
    """LogNotifier durably records to the operator_alerts table (email intentionally
    not built; this is the complete shipped implementation)."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("main_loop", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    esc = RiskEscalationEngine()
    watchdog = _watchdog(db, esc, LogNotifier(db))
    res = watchdog.check(now=RTH_NOW)
    assert res.tripped
    assert read_alert_count(db, kind="heartbeat_stale") == 1


def test_notifier_protocol_is_pluggable() -> None:
    """Any object with notify(alert) satisfies the Notifier protocol -- the plug
    point for a future EmailNotifier without changing the watchdog."""
    assert isinstance(LogNotifier(":memory:"), Notifier)
    assert isinstance(_CapturingNotifier(), Notifier)


def test_stale_message_names_the_actual_component(tmp_path: Path) -> None:
    """F3: the alert message must name the REAL component. The old template hardcoded 'main-loop'
    for every component, so a soak trip read the self-contradictory 'main-loop heartbeat stale (soak)'."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    notifier = _CapturingNotifier()
    wd = HeartbeatWatchdog(db_path=db, escalation=RiskEscalationEngine(), notifier=notifier,
                           stale_threshold_seconds=120.0, component="soak")
    assert wd.check(now=RTH_NOW).tripped
    msg = notifier.alerts[0]["message"]
    assert msg == "soak heartbeat stale"
    assert "main-loop" not in msg


class _ResolvingNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self.resolved: list[tuple] = []

    def notify(self, alert: dict) -> None:
        self.alerts.append(alert)

    def resolve(self, *, component: str, kind: str) -> None:
        self.resolved.append((component, kind))


def test_startup_grace_does_not_page_on_predecessors_stale_heartbeat(tmp_path: Path) -> None:
    """FINDING-4 (INCIDENT-20260722): a fresh process must not page on the PRIOR process's persisted
    heartbeat during the grace window (started_at .. started_at+threshold)."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=17513))  # ancient (prior proc)
    esc = RiskEscalationEngine()
    notifier = _CapturingNotifier()
    wd = HeartbeatWatchdog(db_path=db, escalation=esc, notifier=notifier, stale_threshold_seconds=120.0,
                           component="soak", started_at=RTH_NOW - timedelta(seconds=30))  # 30s ago
    res = wd.check(now=RTH_NOW)
    assert not res.tripped and res.reason == "startup_grace"
    assert not esc.blocks_all_entries()          # no ENTRY_GATE_HALT during grace
    assert notifier.alerts == []                 # no page


def test_grace_expires_then_genuinely_dead_process_trips(tmp_path: Path) -> None:
    """After the grace elapses, a still-stale heartbeat (a genuinely dead new process) trips."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    esc = RiskEscalationEngine()
    wd = HeartbeatWatchdog(db_path=db, escalation=esc, notifier=_CapturingNotifier(),
                           stale_threshold_seconds=120.0, component="soak",
                           started_at=RTH_NOW - timedelta(seconds=300))  # grace already elapsed
    res = wd.check(now=RTH_NOW)
    assert res.tripped and res.reason == "stale_heartbeat"
    assert esc.blocks_all_entries()


# R2 (FINDING-7): bounded watchdog auto-de-escalation.
from src.engine.engine_preemption import RiskEscalationLevel

_PAST_START = RTH_NOW - timedelta(days=1)   # grace long elapsed


def _wd_deesc(db, esc, *, env="paper", enabled=True, streak=10):
    return HeartbeatWatchdog(db_path=db, escalation=esc, notifier=_CapturingNotifier(),
                             stale_threshold_seconds=120.0, component="soak",
                             started_at=_PAST_START, environment=env,
                             auto_deescalation_enabled=enabled, deescalation_fresh_streak=streak)


def test_deescalate_after_sustained_fresh_streak(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    esc = RiskEscalationEngine()
    wd = _wd_deesc(db, esc)
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    assert wd.check(now=RTH_NOW).tripped and esc.blocks_all_entries()   # latched by watchdog
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=10))
    for i in range(9):
        wd.check(now=RTH_NOW)
        assert esc.blocks_all_entries(), f"must stay latched through fresh #{i+1} (<10)"
    wd.check(now=RTH_NOW)                                               # 10th fresh -> de-escalate
    assert esc.snapshot().level == RiskEscalationLevel.NOMINAL
    assert esc.snapshot().commanded_by == "heartbeat_watchdog_recovery"


def test_one_stale_resets_the_streak(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    esc = RiskEscalationEngine()
    wd = _wd_deesc(db, esc)
    t = RTH_NOW
    write_heartbeat_sync("soak", db, beat_utc=t - timedelta(seconds=600))
    wd.check(now=t)                                                    # latch
    for _ in range(9):                                                 # streak -> 9 (not enough)
        t += timedelta(seconds=1)
        write_heartbeat_sync("soak", db, beat_utc=t)
        wd.check(now=t)
    t += timedelta(seconds=700)                                        # advance -> latest HB now stale
    wd.check(now=t)                                                    # stale reading -> resets streak
    for _ in range(9):                                                 # only 9 fresh again
        t += timedelta(seconds=1)
        write_heartbeat_sync("soak", db, beat_utc=t)
        wd.check(now=t)
    assert esc.blocks_all_entries(), "9 fresh after a reset must NOT de-escalate"


def test_never_deescalates_a_non_watchdog_halt(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    esc = RiskEscalationEngine()
    esc.transition(RiskEscalationLevel.ENTRY_GATE_HALT, commanded_by="operator")  # operator-commanded
    wd = _wd_deesc(db, esc)
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=10))
    for _ in range(20):
        wd.check(now=RTH_NOW)
    assert esc.snapshot().level == RiskEscalationLevel.ENTRY_GATE_HALT   # untouched
    assert esc.snapshot().commanded_by == "operator"


def test_live_env_never_deescalates(tmp_path: Path) -> None:
    db = str(tmp_path / "hb.db")
    esc = RiskEscalationEngine()
    wd = _wd_deesc(db, esc, env="live")
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=600))
    wd.check(now=RTH_NOW)                                               # latch (watchdog-commanded)
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=10))
    for _ in range(20):
        wd.check(now=RTH_NOW)
    assert esc.blocks_all_entries(), "live keeps latch-until-human"


def test_healthy_check_signals_resolve(tmp_path: Path) -> None:
    """F5: on a healthy (fresh) heartbeat during RTH the watchdog tells the notifier the condition
    is clear, so a ThrottledNotifier can page ONE 'recovered' and reset its backoff."""
    db = str(tmp_path / "hb.db")
    write_heartbeat_sync("soak", db, beat_utc=RTH_NOW - timedelta(seconds=10))
    notifier = _ResolvingNotifier()
    wd = HeartbeatWatchdog(db_path=db, escalation=RiskEscalationEngine(), notifier=notifier,
                           stale_threshold_seconds=120.0, component="soak")
    res = wd.check(now=RTH_NOW)
    assert not res.tripped and res.reason == "healthy"
    assert notifier.resolved == [("soak", "heartbeat_stale")]
    assert notifier.alerts == []       # healthy path never fires a stale alert
