"""feat/persist-feed-health: additive, dormant feed-health telemetry in engine_operational_state.

A future external deploy-runner reads these to verify the feed is live (G2). No engine decision
consumes them. Tests: fresh feed, staleness grows while the writer keeps advancing, and the
engine-dead case (a stale last_write_utc reveals the ENGINE died, not just the feed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.ingestor.feed_stream_health as fsh
from src.persistence.db import get_feed_health, set_engine_heartbeat_timestamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reset_registry():
    fsh._registry = fsh.FeedStreamHealthRegistry()
    return fsh.get_feed_stream_health_registry()


def test_feed_health_written_fresh_per_stream(tmp_path):
    reg = _reset_registry()
    reg.note_stream_bar("stock")           # QQQ/SPY bar just arrived
    db = tmp_path / "t.db"
    set_engine_heartbeat_timestamp(_now(), db_path=db, feed_health=fsh.build_feed_health_payload())
    fh = get_feed_health(db_path=db)
    assert set(fh) == {"stock", "crypto"}
    assert fh["stock"]["mode"] == "HEALTHY"
    assert fh["stock"]["last_bar_utc"] is not None
    assert fh["stock"]["bar_staleness_seconds"] is not None
    assert fh["stock"]["bar_staleness_seconds"] < 2.0           # fresh
    assert fh["stock"]["last_write_utc"] is not None
    # crypto had no bar -> no last_bar recorded (reader must treat missing as HOLD)
    assert fh["crypto"]["last_bar_utc"] is None
    assert fh["crypto"]["bar_staleness_seconds"] is None


def test_staleness_grows_while_writer_advances(tmp_path, monkeypatch):
    reg = _reset_registry()
    clock = {"t": 1000.0}
    monkeypatch.setattr(fsh.time, "monotonic", lambda: clock["t"])
    reg.note_stream_bar("stock")            # bar at t=1000
    db = tmp_path / "t.db"
    set_engine_heartbeat_timestamp(_now(), db_path=db, feed_health=fsh.build_feed_health_payload())
    p1 = get_feed_health(db_path=db)["stock"]
    clock["t"] = 1120.0                      # 120s later, NO new bar
    set_engine_heartbeat_timestamp(_now(), db_path=db, feed_health=fsh.build_feed_health_payload())
    p2 = get_feed_health(db_path=db)["stock"]
    assert p1["bar_staleness_seconds"] < 2.0
    assert p2["bar_staleness_seconds"] >= 119.0          # gap grew: the bar is frozen
    assert p2["last_write_utc"] >= p1["last_write_utc"]  # the WRITER kept advancing (engine alive)


def test_engine_dead_detectable_via_stale_last_write(tmp_path):
    """The key guard: if the engine dies, last_write_utc stops advancing -> a reader detects
    'engine dead' even when last_bar_utc superficially still looks like a recent bar."""
    reg = _reset_registry()
    reg.note_stream_bar("stock")
    db = tmp_path / "t.db"
    set_engine_heartbeat_timestamp(_now(), db_path=db, feed_health=fsh.build_feed_health_payload())
    p = get_feed_health(db_path=db)["stock"]
    # engine then stops writing; a reader 400s later applies the G2 freshness threshold:
    last_write = datetime.fromisoformat(p["last_write_utc"])
    check_time = datetime.now(timezone.utc) + timedelta(seconds=400)
    engine_alive = (check_time - last_write).total_seconds() < 120.0
    assert engine_alive is False                 # stale last_write_utc reveals the engine is dead
    assert p["last_bar_utc"] is not None          # ...despite last_bar_utc looking superficially fine


def test_heartbeat_write_unaffected_when_no_feed_health(tmp_path):
    """Additivity: the heartbeat write is byte-for-byte unchanged when feed_health is omitted."""
    from src.persistence.db import get_engine_heartbeat_timestamp
    db = tmp_path / "t.db"
    ts = _now()
    set_engine_heartbeat_timestamp(ts, db_path=db)     # no feed_health -> legacy behavior
    assert get_engine_heartbeat_timestamp(db_path=db) == ts
    assert get_feed_health(db_path=db) == {}            # nothing written
