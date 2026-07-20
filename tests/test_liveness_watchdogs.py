"""P3.2/3.3 — soak + mirror_sync watchdogs and the composite notifier wiring.

Verifies: a stale heartbeat trips + notifies; soak escalates (block-new-entries) but
mirror_sync does NOT (notify-only — a lagging mirror must never halt trading); and the
composite fires both the durable log row and the email page.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from src.alerts.email_dispatcher import build_log_and_email
from src.control.liveness_watchdogs import (
    NullEscalation, build_mirror_sync_watchdog, build_soak_watchdog,
)
from src.persistence.heartbeat_store import write_heartbeat_sync


class _AlwaysOpen:
    def is_within_rth(self, now):
        return True


class _SpyEscalation:
    def __init__(self):
        self.transitions = []

    def transition(self, level, **kwargs):
        self.transitions.append((level, kwargs))
        return None


class _SpyNotifier:
    def __init__(self):
        self.alerts = []

    def notify(self, alert):
        self.alerts.append(alert)


def _stale(db, component):
    write_heartbeat_sync(component, db, beat_utc=datetime.now(timezone.utc) - timedelta(hours=5))


def test_soak_watchdog_trips_escalates_and_notifies(tmp_path):
    db = tmp_path / "rv.db"
    _stale(db, "soak")
    esc, notif = _SpyEscalation(), _SpyNotifier()
    wd = build_soak_watchdog(db_path=db, escalation=esc, notifier=notif)
    wd._calendar = _AlwaysOpen()
    wd._started_at = datetime.now(timezone.utc) - timedelta(hours=1)   # FINDING-4 grace elapsed
    res = wd.check()
    assert res.tripped is True
    assert len(esc.transitions) == 1              # soak halts entries
    assert notif.alerts and notif.alerts[0]["detail"]["component"] == "soak"


def test_mirror_sync_watchdog_notifies_but_never_halts(tmp_path):
    """A stale mirror alerts but must NOT touch trading risk state (NullEscalation)."""
    db = tmp_path / "rv.db"
    _stale(db, "mirror_sync")
    notif = _SpyNotifier()
    wd = build_mirror_sync_watchdog(db_path=db, notifier=notif)
    wd._calendar = _AlwaysOpen()
    wd._started_at = datetime.now(timezone.utc) - timedelta(hours=1)   # FINDING-4 grace elapsed
    res = wd.check()
    assert res.tripped is True                    # it DOES alert
    assert notif.alerts[0]["detail"]["component"] == "mirror_sync"
    assert isinstance(wd._escalation, NullEscalation)   # ...via a no-op escalation


def test_soak_healthy_across_rth_window_with_60s_beats(tmp_path):
    """F2 acceptance (both directions, healthy leg): with the soak beat written every ~60s -- the
    lightweight-pass producer fix -- the 120s watchdog NEVER trips across a full RTH window. Before
    the fix, beats came only every ~15min (leg-due cycles), so the watchdog false-tripped and forced
    ENTRY_GATE_HALT for ~13 of every 15 minutes during RTH."""
    db = tmp_path / "rv.db"
    esc, notif = _SpyEscalation(), _SpyNotifier()
    wd = build_soak_watchdog(db_path=db, escalation=esc, notifier=notif)
    wd._calendar = _AlwaysOpen()
    base = datetime.now(timezone.utc)
    for i in range(15):                                   # 15 minutes of 60s beats
        beat = base + timedelta(seconds=60 * i)
        write_heartbeat_sync("soak", db, beat_utc=beat)
        res = wd.check(now=beat + timedelta(seconds=59))  # poll just before the next beat: age<120s
        assert not res.tripped, f"false trip at minute {i} (age 59s < 120s threshold)"
    assert esc.transitions == [] and notif.alerts == []   # never halted, never paged


def test_fresh_heartbeat_does_not_trip(tmp_path):
    db = tmp_path / "rv.db"
    write_heartbeat_sync("soak", db, beat_utc=datetime.now(timezone.utc))
    esc, notif = _SpyEscalation(), _SpyNotifier()
    wd = build_soak_watchdog(db_path=db, escalation=esc, notifier=notif)
    wd._calendar = _AlwaysOpen()
    assert wd.check().tripped is False
    assert esc.transitions == [] and notif.alerts == []


def test_stale_trip_fires_both_log_and_email(tmp_path, monkeypatch):
    """Composite: a trip lands a durable operator_alerts row AND pages email."""
    for k, v in {"SMTP_HOST": "smtp.test", "EMAIL_FROM": "a@test", "EMAIL_TO": "b@test"}.items():
        monkeypatch.setenv(k, v)
    db = tmp_path / "rv.db"
    _stale(db, "soak")
    sent = []

    async def send_fn(subject, body, cfg):
        sent.append(subject)

    notifier = build_log_and_email(db, send_fn=send_fn)

    async def scenario():
        wd = build_soak_watchdog(db_path=db, escalation=_SpyEscalation(), notifier=notifier)
        wd._calendar = _AlwaysOpen()
        wd._started_at = datetime.now(timezone.utc) - timedelta(hours=1)   # FINDING-4 grace elapsed
        wd.check()
        await asyncio.sleep(0.05)   # let the fire-and-forget email task run
    asyncio.run(scenario())

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM operator_alerts").fetchone()[0]
    assert rows >= 1               # durable row
    assert len(sent) == 1          # email page
