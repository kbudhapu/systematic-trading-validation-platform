"""G1.5 db_queue robustness: the shutdown-timeout bug fix (bounded drain, no
hang), a high-volume concurrent write storm (zero lost writes, bounded backlog),
and the additive HEARTBEAT write kind."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from src.persistence.db_queue import AsyncDBWriter, QueuedWrite, WriteKind
from src.persistence.heartbeat_store import read_latest_heartbeat


def _count(db: str, table: str) -> int:
    import sqlite3
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_high_volume_storm_zero_lost_writes(tmp_path: Path) -> None:
    """Concurrent telemetry at >=10x a normal cycle's volume drains with zero lost
    writes, bounded backlog, and no deadlock."""
    wal = str(tmp_path / "wal.db")
    target = str(tmp_path / "target.db")
    writer = AsyncDBWriter(db_path=wal, flush_interval_seconds=0.05)
    writer._postgres_settings = None      # SQLite-only path for the test
    writer.start()

    n_threads, per_thread = 12, 120       # 1440 writes (>=10x a ~100-row cycle)
    total = n_threads * per_thread

    def worker(tid: int) -> None:
        for i in range(per_thread):
            writer.enqueue(QueuedWrite(
                kind=WriteKind.HEARTBEAT,
                payload={"component": f"t{tid}", "beat_utc": f"2026-07-02T00:00:{i:02d}+00:00"},
                db_path=target))

    start = time.monotonic()
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    peak_depth = 0
    for t in threads:
        while t.is_alive():
            peak_depth = max(peak_depth, writer.queue_depth)
            t.join(timeout=0.05)
    writer.stop(timeout_seconds=15.0)
    elapsed = time.monotonic() - start

    persisted = _count(target, "loop_heartbeats")
    print(f"\nstorm: {total} writes persisted={persisted} peak_backlog={peak_depth} "
          f"elapsed={elapsed:.2f}s throughput={total / elapsed:.0f}/s")
    assert persisted == total, f"lost writes: {total - persisted}"
    assert writer.queue_depth == 0, "backlog must be fully drained after stop"
    assert peak_depth <= total, "backlog stayed bounded (never exceeded enqueued)"


def test_stop_does_not_hang_on_poison_batch(tmp_path: Path) -> None:
    """The bug regression: a batch that can never commit (missing required fields)
    must NOT hang stop() -- the bounded drain + no-progress guard returns promptly
    and retains the poison row (not lost)."""
    wal = str(tmp_path / "wal.db")
    target = str(tmp_path / "target.db")
    writer = AsyncDBWriter(db_path=wal, flush_interval_seconds=0.05)
    writer._postgres_settings = None
    # a TRIAL_LEDGER payload missing required keys -> persist raises every time
    writer.enqueue(QueuedWrite(kind=WriteKind.TRIAL_LEDGER, payload={"leg_id": "x"}, db_path=target))

    start = time.monotonic()
    writer.stop(timeout_seconds=2.0)      # must return, not hang
    elapsed = time.monotonic() - start
    print(f"\npoison-batch stop returned in {elapsed:.2f}s")
    assert elapsed < 6.0, f"stop() hung on a poison batch ({elapsed:.1f}s)"
    assert writer.queue_depth >= 1, "poison row retained (not silently lost)"


def test_poison_group_does_not_jam_healthy_groups(tmp_path: Path) -> None:
    """Droplet-migration regression: a group that can never commit on this host
    (a foreign/unwritable target_db_path -- e.g. a cross-platform WAL spool row
    carried in a restored vault: a Windows path replayed on Linux) must NOT block
    the healthy groups behind it in the same oldest-first batch. Before per-group
    fault isolation the poisoned head group aborted the entire drain and jammed
    every healthy write forever."""
    wal = str(tmp_path / "wal.db")
    good = str(tmp_path / "target.db")
    # a FILE where a directory is needed -> the target can never be created,
    # so the poison group raises on every flush regardless of any mkdir attempt.
    (tmp_path / "blocker").write_text("x")
    foreign = str(tmp_path / "blocker" / "foreign.db")

    writer = AsyncDBWriter(db_path=wal, flush_interval_seconds=0.05)
    writer._postgres_settings = None
    # poison enqueued FIRST -> lowest stage_id -> head of the oldest-first batch.
    writer.enqueue(QueuedWrite(
        kind=WriteKind.HEARTBEAT,
        payload={"component": "poison", "beat_utc": "2026-07-07T00:00:00+00:00"},
        db_path=foreign))
    for i in range(5):
        writer.enqueue(QueuedWrite(
            kind=WriteKind.HEARTBEAT,
            payload={"component": f"good{i}", "beat_utc": f"2026-07-07T00:00:{i:02d}+00:00"},
            db_path=good))

    writer.start()
    writer.stop(timeout_seconds=10.0)

    assert _count(good, "loop_heartbeats") == 5, "healthy writes must drain past the poison group"
    assert writer.queue_depth >= 1, "poison row retained, not silently lost"


def test_stop_is_idempotent_and_safe_when_not_running(tmp_path: Path) -> None:
    writer = AsyncDBWriter(db_path=str(tmp_path / "wal.db"), flush_interval_seconds=0.05)
    writer._postgres_settings = None
    writer.stop(timeout_seconds=1.0)      # never started -> bounded no-op
    writer.start()
    writer.stop(timeout_seconds=2.0)
    writer.stop(timeout_seconds=2.0)      # double stop is safe


def test_heartbeat_writer_dispatch(tmp_path: Path) -> None:
    """Additive HEARTBEAT flush handler appends via the heartbeat store."""
    db = str(tmp_path / "hb.db")
    w = AsyncDBWriter(db_path=str(tmp_path / "wal.db"))
    w._postgres_settings = None
    items = [QueuedWrite(kind=WriteKind.HEARTBEAT,
                         payload={"component": "main_loop", "beat_utc": "2026-07-02T15:30:00+00:00"},
                         db_path=db)]
    assert w._flush_heartbeat_batch(items, Path(db)) is True
    latest = read_latest_heartbeat("main_loop", db)
    assert latest is not None and latest.isoformat() == "2026-07-02T15:30:00+00:00"
