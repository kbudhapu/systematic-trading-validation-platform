"""P2.3 — mirror-freshness marker + staleness surfacing.

Locks the behaviour that prevents this week's false P0: a report reading a stale copy
of research_vault.db must report the data's age and flag it stale, never silently
return old numbers. No network, no droplet — pure SQLite fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.control.mirror_freshness import (
    DEFAULT_STALE_AFTER_S, mirror_freshness, read_last_synced, record_sync,
)
from src.persistence.heartbeat_store import write_heartbeat_sync


def _db(tmp_path):
    return tmp_path / "research_vault.db"


def test_fresh_heartbeat_is_not_stale(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    write_heartbeat_sync("sip_collector", db, beat_utc=now - timedelta(minutes=2))
    f = mirror_freshness(db, now=now)
    assert f.source == "heartbeat"
    assert f.is_stale is False
    assert f.age_seconds < DEFAULT_STALE_AFTER_S
    assert "STALE" not in f.banner()


def test_old_heartbeat_is_stale_and_reports_age(tmp_path):
    """The exact false-P0 shape: heartbeat frozen 5 days ago on a stale mirror copy."""
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    write_heartbeat_sync("sip_collector", db, beat_utc=now - timedelta(days=5))
    f = mirror_freshness(db, now=now)
    assert f.source == "heartbeat"
    assert f.is_stale is True
    assert f.age_seconds == pytest.approx(5 * 86400, rel=0.01)
    assert "STALE" in f.banner() and "5.0d" in f.banner()


def test_sync_marker_overrides_heartbeat(tmp_path):
    """An explicit recent sync marker wins over an old heartbeat (source=sync_meta)."""
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    write_heartbeat_sync("sip_collector", db, beat_utc=now - timedelta(days=5))
    record_sync(db, now=now - timedelta(minutes=1))
    f = mirror_freshness(db, now=now)
    assert f.source == "sync_meta"
    assert f.is_stale is False
    assert read_last_synced(db) is not None


def test_empty_db_is_unknown_and_treated_stale(tmp_path):
    """No marker and no heartbeat -> UNKNOWN age, flagged stale (never silently trusted)."""
    db = _db(tmp_path)
    write_heartbeat_sync("sip_collector", db, beat_utc=datetime.now(timezone.utc))
    # wrong component -> no matching heartbeat, no sync marker
    f = mirror_freshness(db, heartbeat_component="does_not_exist", now=datetime.now(timezone.utc))
    assert f.source == "none"
    assert f.as_of is None and f.age_seconds is None
    assert f.is_stale is True
    assert "UNKNOWN" in f.banner()


def test_missing_db_file_is_unknown(tmp_path):
    f = mirror_freshness(tmp_path / "nope.db", now=datetime.now(timezone.utc))
    assert f.source == "none" and f.is_stale is True


def test_caller_threshold_is_respected(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    write_heartbeat_sync("sip_collector", db, beat_utc=now - timedelta(minutes=20))
    assert mirror_freshness(db, stale_after_seconds=600, now=now).is_stale is True   # 2x 5m
    assert mirror_freshness(db, stale_after_seconds=3600, now=now).is_stale is False


def test_operator_report_surfaces_stale_banner(tmp_path):
    """The report must carry the age banner + a stale warning when the DB is old."""
    from src.control.operator_report import build_report, render_report
    db = _db(tmp_path)
    write_heartbeat_sync("sip_collector", db, beat_utc=datetime.now(timezone.utc) - timedelta(days=5))
    rendered = render_report(build_report(db))
    assert "data as of" in rendered and "STALE" in rendered
    assert "WARNING: the source DB is stale" in rendered
