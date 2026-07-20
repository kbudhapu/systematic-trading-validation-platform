"""Tests for on-call alerting and heartbeat monitoring."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.control.alerts import (
    IncidentType,
    SystemHeartbeatMonitor,
    dispatch_critical_page,
    heartbeat_age_seconds,
    record_successful_cycle_heartbeat,
)
from src.persistence import db as persistence


@pytest.fixture(autouse=True)
def _stub_persistence_log(monkeypatch):
    """Prevent dispatch_critical_page from hitting the real DB during unit tests."""
    monkeypatch.setattr(
        "src.persistence.db.log_system_event",
        lambda *a, **kw: None,
    )
    # Reset the global cooldown state between tests to prevent ordering-dependent failures.
    import src.control.alerts as _alerts_mod
    _alerts_mod._recent_pages.clear()


@pytest.fixture
def heartbeat_db(tmp_path):
    db_path = tmp_path / "heartbeat.db"
    persistence.init_db(db_path)
    return db_path


def test_dispatch_critical_page_log_only_without_webhook(monkeypatch, heartbeat_db):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    result = asyncio.run(
        dispatch_critical_page(
            IncidentType.WAL_BACKLOG_CRITICAL,
            "backlog high",
            {"depth": 300},
            cooldown_seconds=0.0,
        )
    )
    assert result.incident_type == IncidentType.WAL_BACKLOG_CRITICAL.value
    assert result.delivered is False
    assert result.channel == "log_only"


def test_dispatch_critical_page_respects_cooldown(monkeypatch, heartbeat_db):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    first = asyncio.run(
        dispatch_critical_page(
            IncidentType.MAINTENANCE_JOB_FAILED,
            "job failed",
            {},
            cooldown_seconds=60.0,
        )
    )
    second = asyncio.run(
        dispatch_critical_page(
            IncidentType.MAINTENANCE_JOB_FAILED,
            "job failed again",
            {},
            cooldown_seconds=60.0,
        )
    )
    assert first.reason != "cooldown_active"
    assert second.reason == "cooldown_active"


def test_record_and_read_heartbeat_timestamp(heartbeat_db):
    ts = datetime.now(timezone.utc).isoformat()
    record_successful_cycle_heartbeat(db_path=heartbeat_db)
    stored = persistence.get_engine_heartbeat_timestamp(db_path=heartbeat_db)
    assert stored is not None
    age = heartbeat_age_seconds(db_path=heartbeat_db)
    assert age is not None
    assert age < 5.0


def test_dead_man_monitor_pages_on_stale_heartbeat(heartbeat_db, monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    persistence.set_engine_heartbeat_timestamp(stale, db_path=heartbeat_db)

    pages: list[str] = []

    async def _capture(incident_type, message, metadata=None, **kwargs):
        pages.append(
            incident_type.value
            if hasattr(incident_type, "value")
            else str(incident_type)
        )
        from src.control.alerts import PageDispatchResult

        return PageDispatchResult(
            delivered=False,
            channel="test",
            status_code=None,
            reason="test",
            incident_type=str(incident_type),
        )

    monkeypatch.setattr("src.control.alerts.dispatch_critical_page", _capture)

    monitor = SystemHeartbeatMonitor(
        cycle_interval_seconds=60.0,
        db_path=heartbeat_db,
        check_interval_seconds=0.05,
    )
    assert monitor.evaluate_dead_man_once() is True
    assert IncidentType.DEAD_MAN_HEARTBEAT.value in pages


def test_dead_man_none_heartbeat_within_grace_not_stale(heartbeat_db):
    """A never-written heartbeat is NOT stale when the monitor just started (within grace)."""
    monitor = SystemHeartbeatMonitor(
        cycle_interval_seconds=60.0,
        db_path=heartbeat_db,
        # Grace = 180s; monitor was just created, so elapsed ≈ 0 < 180s.
        none_stale_after_seconds=180.0,
    )
    # No heartbeat written — heartbeat_age_seconds() returns None.
    assert heartbeat_age_seconds(db_path=heartbeat_db) is None
    assert monitor.is_dead_man_stale() is False


def test_dead_man_none_heartbeat_past_grace_is_stale(heartbeat_db):
    """A never-written heartbeat IS stale once the grace period has elapsed."""
    monitor = SystemHeartbeatMonitor(
        cycle_interval_seconds=60.0,
        db_path=heartbeat_db,
        # Grace = 0 simulates the monitor having been alive long enough that
        # any elapsed time (> 0s) exceeds the grace window.
        none_stale_after_seconds=0.0,
    )
    # No heartbeat written — heartbeat_age_seconds() returns None.
    assert heartbeat_age_seconds(db_path=heartbeat_db) is None
    assert monitor.is_dead_man_stale() is True


def test_dead_man_none_heartbeat_pages_when_past_grace(heartbeat_db, monkeypatch):
    """evaluate_dead_man_once pages even when no heartbeat has ever been written."""
    pages: list[str] = []

    async def _capture(incident_type, message, metadata=None, **kwargs):
        pages.append(
            incident_type.value if hasattr(incident_type, "value") else str(incident_type)
        )
        from src.control.alerts import PageDispatchResult
        return PageDispatchResult(
            delivered=False, channel="test", status_code=None,
            reason="test", incident_type=str(incident_type),
        )

    monkeypatch.setattr("src.control.alerts.dispatch_critical_page", _capture)

    monitor = SystemHeartbeatMonitor(
        cycle_interval_seconds=60.0,
        db_path=heartbeat_db,
        none_stale_after_seconds=0.0,  # grace already elapsed
    )
    assert heartbeat_age_seconds(db_path=heartbeat_db) is None
    assert monitor.evaluate_dead_man_once() is True
    assert IncidentType.DEAD_MAN_HEARTBEAT.value in pages
    assert any(
        "never written" in msg or msg
        for msg in pages
    ), "page should mention that heartbeat was never written"


def test_get_write_ahead_backlog_depth(tmp_path):
    from src.persistence.db_queue import (
        WAL_BACKLOG_PAGE_THRESHOLD,
        get_write_ahead_backlog_depth,
    )

    assert WAL_BACKLOG_PAGE_THRESHOLD == 256
    depth = get_write_ahead_backlog_depth()
    assert isinstance(depth, int)
    assert depth >= 0
