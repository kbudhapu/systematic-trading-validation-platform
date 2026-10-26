"""K1: control-command lifecycle -- complete-after-durable-success + crash recovery.

The bug (prior session): kill/config commands executed but were left non-terminal
('processing'), because FLATTEN completion was ordered after the shutdown latch and
gated on the command object. Fix semantic:
  - complete ONLY after the effect is durably applied (never claim-time);
  - a crash mid-execution leaves the command re-fireable (boot reclaim re-queues it);
  - a completed/failed command is TERMINAL and never re-fires on any later boot;
  - the protective action (flatten against a flat book / config reload) is idempotent.

These tests drive a SYNTHETIC bus (temp SQLite, no Supabase) with a mock idempotent
action, covering both FLATTEN_AND_HALT and RELOAD_CONFIG across the three cases:
(a/d) normal, (b/e) crash-mid -> reboot -> re-fire once -> terminal, (c/f) reboot
with a terminal command -> no-op.
"""
from __future__ import annotations

import sqlite3

import pytest

import src.control.command_queue as cq
from src.control.command_queue import (
    ControlCommandType,
    enqueue_control_command_local,
    fetch_pending_commands,
    mark_command_completed,
    reclaim_orphaned_processing_commands,
    try_claim_command,
)


@pytest.fixture()
def bus(tmp_path, monkeypatch):
    db = tmp_path / "cmd.db"
    monkeypatch.setattr(cq, "DB_PATH", db)
    monkeypatch.setattr(cq, "get_supabase", lambda: None)   # local SQLite bus only
    cq.ensure_control_commands_schema(db)
    return db


def _row(db, cid):
    with sqlite3.connect(db) as c:
        return c.execute("SELECT status, attempts FROM control_commands WHERE command_id=?",
                         (cid,)).fetchone()


class MockBook:
    """Positions with an IDEMPOTENT flatten (flatten-against-flat-book is a no-op)."""

    def __init__(self):
        self.positions = ["QQQ", "BTC"]
        self.flatten_calls = 0
        self.noop_flattens = 0

    def flatten(self) -> str:
        self.flatten_calls += 1
        if not self.positions:
            self.noop_flattens += 1
            return "noop_already_flat"
        self.positions = []
        return "flattened"


# --------------------------------------------------------------------------- #
# FLATTEN_AND_HALT
# --------------------------------------------------------------------------- #
def test_a_flatten_normal_lifecycle(bus):
    book = MockBook()
    cmd = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT, {})
    assert _row(bus, cmd.command_id)[0] == "pending"
    assert try_claim_command(cmd.command_id)                 # -> processing
    assert _row(bus, cmd.command_id)[0] == "processing"
    book.flatten()                                           # durable action
    mark_command_completed(cmd.command_id)                   # complete-after-success
    assert _row(bus, cmd.command_id)[0] == "completed"
    assert book.positions == [] and book.flatten_calls == 1


def test_b_flatten_crash_mid_execution_refires_once(bus):
    book = MockBook()
    cmd = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT, {})
    assert try_claim_command(cmd.command_id)                 # claimed -> processing
    # CRASH before durable success: process dies, no execution, no completion.
    assert _row(bus, cmd.command_id) == ("processing", 0)

    # Reboot: the crash-recovery reclaim re-queues the orphan (re-fireable).
    assert reclaim_orphaned_processing_commands(db_path=bus) == 1
    assert _row(bus, cmd.command_id) == ("pending", 1)

    # Re-fire exactly once through the normal path -> execute -> complete -> terminal.
    pend = [c for c in _local_pending(bus) if c.command_id == cmd.command_id]
    assert len(pend) == 1
    assert try_claim_command(cmd.command_id)
    book.flatten()
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed"
    assert book.flatten_calls == 1                           # executed exactly once

    # flatten-against-a-flat-book is a harmless no-op (idempotent re-execution).
    assert book.flatten() == "noop_already_flat"
    assert book.noop_flattens == 1


def test_c_flatten_reboot_with_terminal_is_noop(bus):
    book = MockBook()
    cmd = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT, {})
    try_claim_command(cmd.command_id)
    book.flatten()
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed"

    # Reboot: reclaim must NOT touch a terminal command -> zero re-fire.
    assert reclaim_orphaned_processing_commands(db_path=bus) == 0
    assert _row(bus, cmd.command_id)[0] == "completed"
    assert not [c for c in _local_pending(bus) if c.command_id == cmd.command_id]
    # re-completing a terminal command is an idempotent no-op.
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed"
    assert book.flatten_calls == 1                           # never re-executed


# --------------------------------------------------------------------------- #
# RELOAD_CONFIG (same three cases)
# --------------------------------------------------------------------------- #
def test_d_reload_normal_lifecycle(bus):
    reloads = []
    cmd = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})
    assert try_claim_command(cmd.command_id)
    reloads.append(1)                                        # durable reload
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed" and len(reloads) == 1


def test_e_reload_crash_mid_execution_refires_once(bus):
    reloads = []
    cmd = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})
    assert try_claim_command(cmd.command_id)                 # crash before reload/complete
    assert _row(bus, cmd.command_id) == ("processing", 0)
    assert reclaim_orphaned_processing_commands(db_path=bus) == 1
    assert _row(bus, cmd.command_id) == ("pending", 1)
    assert try_claim_command(cmd.command_id)
    reloads.append(1)
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed" and len(reloads) == 1


def test_f_reload_reboot_with_terminal_is_noop(bus):
    cmd = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})
    try_claim_command(cmd.command_id)
    mark_command_completed(cmd.command_id)
    assert reclaim_orphaned_processing_commands(db_path=bus) == 0
    assert _row(bus, cmd.command_id)[0] == "completed"


def test_poison_command_is_bounded(bus):
    """A command that crashes every attempt is failed after MAX_RECLAIM_ATTEMPTS, not
    re-queued forever."""
    cmd = enqueue_control_command_local(ControlCommandType.FLATTEN_AND_HALT, {})
    for _ in range(cq.MAX_RECLAIM_ATTEMPTS):
        assert try_claim_command(cmd.command_id)             # claim, then "crash"
        reclaim_orphaned_processing_commands(db_path=bus)    # re-queue (bounded)
    # attempts now == MAX; one more claim+reclaim marks it failed (terminal), no re-queue.
    assert try_claim_command(cmd.command_id)
    assert reclaim_orphaned_processing_commands(db_path=bus) == 0
    assert _row(bus, cmd.command_id)[0] == "failed"


# --------------------------------------------------------------------------- #
# PC-1: poison-path repair. A SUCCESSFUL dead-letter is not an error; only a
# dead-letter that FAILS to land (rowcount 0) is -- and it must page.
# --------------------------------------------------------------------------- #
def _drive_to_poison_ready(bus, command_type=ControlCommandType.RELOAD_CONFIG):
    """Return a command left in `processing` with attempts == MAX (the next reclaim poisons it)."""
    cmd = enqueue_control_command_local(command_type, {})
    for _ in range(cq.MAX_RECLAIM_ATTEMPTS):
        assert try_claim_command(cmd.command_id)
        reclaim_orphaned_processing_commands(db_path=bus)
    assert try_claim_command(cmd.command_id)
    assert _row(bus, cmd.command_id) == ("processing", cq.MAX_RECLAIM_ATTEMPTS)
    return cmd


def test_successful_poison_logs_poisoned_not_failed(bus):
    """The 172-false-alarms bug: a successful dead-letter logged `control_command_poison_failed`
    at ERROR. It must now log `control_command_poisoned` and NOT the failure event."""
    from structlog.testing import capture_logs

    cmd = _drive_to_poison_ready(bus)
    with capture_logs() as logs:
        assert reclaim_orphaned_processing_commands(db_path=bus) == 0   # poison, no re-queue
    events = [e["event"] for e in logs]
    assert "control_command_poisoned" in events, events
    assert "control_command_poison_failed" not in events, "a successful poison must NOT read as a failure"
    assert _row(bus, cmd.command_id)[0] == "failed"


def test_dead_lettered_command_never_repolls(bus):
    """Once dead-lettered, a command is terminal: it cannot be claimed and a later reclaim is a no-op."""
    cmd = _drive_to_poison_ready(bus)
    reclaim_orphaned_processing_commands(db_path=bus)             # poison -> failed
    assert _row(bus, cmd.command_id)[0] == "failed"
    assert not try_claim_command(cmd.command_id), "a failed command must never be re-claimable"
    assert reclaim_orphaned_processing_commands(db_path=bus) == 0
    assert _row(bus, cmd.command_id)[0] == "failed"


class _SpyNotifier:
    def __init__(self):
        self.alerts: list[dict] = []

    def notify(self, alert: dict) -> None:
        self.alerts.append(alert)


def test_genuine_poison_failure_pages(bus, monkeypatch):
    """PC-1d: when the dead-letter UPDATE matches 0 rows (a command could NOT be retired),
    it logs `control_command_poison_failed` AND pages the notifier at critical severity."""
    from structlog.testing import capture_logs

    cmd = _drive_to_poison_ready(bus)
    real_connect = cq.sqlite3.connect

    class _Conn:  # forces the poison (status->'failed') UPDATE to report rowcount 0
        def __init__(self, c):
            object.__setattr__(self, "_c", c)

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *a):
            return self._c.__exit__(*a)

        def __getattr__(self, n):
            return getattr(self._c, n)

        def __setattr__(self, n, v):
            setattr(self._c, n, v)

        def execute(self, sql, params=()):
            cur = self._c.execute(sql, params)
            if sql.lstrip().upper().startswith("UPDATE") and params and \
                    params[0] == cq.ControlCommandStatus.FAILED.value:
                class _Zero:
                    rowcount = 0
                return _Zero()
            return cur

    monkeypatch.setattr(cq.sqlite3, "connect", lambda p, *a, **k: _Conn(real_connect(p, *a, **k)))
    spy = _SpyNotifier()
    with capture_logs() as logs:
        reclaim_orphaned_processing_commands(db_path=bus, notifier=spy)
    events = [e["event"] for e in logs]
    assert "control_command_poison_failed" in events, events
    assert spy.alerts, "a genuine dead-letter failure must PAGE, not just log"
    assert spy.alerts[0]["kind"] == "control_command_poison_failed"
    assert spy.alerts[0]["severity"] == "critical"
    assert cmd.command_id in spy.alerts[0]["detail"]["command_ids"]


def test_poison_page_helper_none_notifier_is_safe():
    """Paging must never raise into crash-recovery when no notifier is wired."""
    cq._page_poison_failures(None, ["abc"])   # must not raise


def test_poison_page_helper_swallows_notifier_error():
    class _Boom:
        def notify(self, alert):
            raise RuntimeError("channel down")

    cq._page_poison_failures(_Boom(), ["abc"])   # must not raise


# --------------------------------------------------------------------------- #
# PB: PING probe + TTL/expiry. An expired probe is an EXPECTED terminal, retired
# quietly -- never poisoned, never paged. TTL is forbidden on safety commands.
# --------------------------------------------------------------------------- #
def _past():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _future():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def test_ping_round_trip(bus):
    cmd = enqueue_control_command_local(
        ControlCommandType.PING, {"soak_probe": True},
        requested_by="paper_soak_runner", expires_at=_future())
    assert cmd.expires_at is not None
    assert try_claim_command(cmd.command_id)          # claim
    mark_command_completed(cmd.command_id)             # no-op execute -> terminal
    assert _row(bus, cmd.command_id)[0] == "completed"


def test_orphaned_expired_ping_self_retires_quietly(bus):
    """The whole point: a probe orphaned past its TTL retires 'expired' with NO error/page."""
    from src.control.command_queue import retire_expired_commands
    from structlog.testing import capture_logs

    cmd = enqueue_control_command_local(
        ControlCommandType.PING, {"soak_probe": True}, expires_at=_past())
    assert try_claim_command(cmd.command_id)          # -> processing (orphaned, runner "died")
    with capture_logs() as logs:
        assert retire_expired_commands(db_path=bus) == 1
    events = [e["event"] for e in logs]
    assert "control_commands_expired" in events
    assert "control_command_poison_failed" not in events
    assert not any(e.get("log_level") == "error" for e in logs), "expiry must never log ERROR"
    with sqlite3.connect(bus) as c:
        status, err = c.execute(
            "SELECT status, error_message FROM control_commands WHERE command_id=?",
            (cmd.command_id,)).fetchone()
    assert status == "failed" and "expired" in err
    assert not try_claim_command(cmd.command_id), "an expired-retired command must not re-poll"


def test_expiry_never_touches_non_expired_or_null_ttl(bus):
    from src.control.command_queue import retire_expired_commands
    future_ping = enqueue_control_command_local(
        ControlCommandType.PING, {}, expires_at=_future())
    assert try_claim_command(future_ping.command_id)          # processing, not yet expired
    null_ttl = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})  # NULL expires_at
    assert try_claim_command(null_ttl.command_id)
    assert retire_expired_commands(db_path=bus) == 0
    assert _row(bus, future_ping.command_id)[0] == "processing"
    assert _row(bus, null_ttl.command_id)[0] == "processing"


def test_ttl_forbidden_on_safety_commands(bus):
    for dangerous in (ControlCommandType.FLATTEN_AND_HALT, ControlCommandType.ENGAGE_KILL_SWITCH):
        with pytest.raises(ValueError):
            enqueue_control_command_local(dangerous, {}, expires_at=_future())


def test_expiry_sweep_does_not_disturb_poison_path(bus):
    """PB/PC-1 coexistence: a NULL-TTL command still poisons normally after N attempts;
    the expiry sweep leaves it alone."""
    from src.control.command_queue import retire_expired_commands
    cmd = _drive_to_poison_ready(bus)                          # RELOAD_CONFIG, NULL expires_at
    assert retire_expired_commands(db_path=bus) == 0           # not expired -> untouched
    reclaim_orphaned_processing_commands(db_path=bus)          # poison ladder still fires
    assert _row(bus, cmd.command_id)[0] == "failed"


def test_retire_stale_orphans_preserves_audit_rows(bus):
    """PC-1c: retirement transitions a STALE processing orphan to `failed` with an audit
    stamp (row preserved), never a DELETE; a FRESH in-flight command is protected by the
    age gate."""
    stale = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})
    assert try_claim_command(stale.command_id)                       # -> processing
    # Backdate the stale row's created_at so it is older than the age gate.
    with sqlite3.connect(bus) as c:
        c.execute("UPDATE control_commands SET created_at = ? WHERE command_id = ?",
                  ("2026-07-14T21:06:24+00:00", stale.command_id))
    fresh = enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, {})
    assert try_claim_command(fresh.command_id)                       # -> processing, just now

    n = cq.retire_stale_processing_orphans(
        reason="PC-1c backlog", older_than_minutes=10.0,
        command_type=ControlCommandType.RELOAD_CONFIG, db_path=bus)
    assert n == 1
    # stale -> failed, ROW STILL EXISTS with an audit stamp (not deleted).
    with sqlite3.connect(bus) as c:
        srow = c.execute("SELECT status, error_message FROM control_commands WHERE command_id=?",
                         (stale.command_id,)).fetchone()
    assert srow is not None and srow[0] == "failed" and "retired: PC-1c backlog" in srow[1]
    # fresh in-flight command is UNTOUCHED (age gate protects it).
    assert _row(bus, fresh.command_id)[0] == "processing"
    # retired command never re-polls.
    assert not try_claim_command(stale.command_id)


def _local_pending(db):
    """Pending commands from the local SQLite bus only (get_supabase is stubbed None)."""
    return fetch_pending_commands()


# --------------------------------------------------------------------------- #
# CQ-1: mark_command_completed/failed must FALL THROUGH to local SQLite when the
# Supabase update matches 0 rows (the 36-stuck-PING store-split regression).
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, remote_ids):
        self._remote_ids = remote_ids
        self._cid = None

    def update(self, _vals):
        return self

    def eq(self, col, val):
        if col == "command_id":
            self._cid = val
        return self

    def execute(self):
        return _FakeResp([{"command_id": self._cid}] if self._cid in self._remote_ids else [])


class _FakeSupabase:
    def __init__(self, remote_ids):
        self._remote_ids = remote_ids

    def table(self, _name):
        return _FakeQuery(self._remote_ids)


def test_cq1_local_only_command_completes_local_when_supabase_misses(bus, monkeypatch):
    """The 36-orphan regression: Supabase present but the command is LOCAL-ONLY -> Supabase update
    hits 0 rows -> must fall through and complete LOCALLY."""
    monkeypatch.setattr(cq, "get_supabase", lambda: _FakeSupabase(set()))  # remote has nothing
    cmd = enqueue_control_command_local(ControlCommandType.PING, {"soak_probe": True})
    assert try_claim_command(cmd.command_id)                    # -> processing (local)
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed", "must complete LOCAL when Supabase misses"


def test_cq1_supabase_hit_mirrors_terminal_to_local(bus, monkeypatch):
    """E2: a Supabase hit MIRRORS the terminal status FORWARD to the local copy too, so the two
    buses agree (previously the local mirror was left 'processing'). The CQ1 concern — a
    local-only command must still be written on a Supabase MISS — is preserved by the miss→local
    fall-through (test_cq1_genuine_double_miss_logs_error / already-terminal-noop below)."""
    cmd = enqueue_control_command_local(ControlCommandType.PING, {})
    assert try_claim_command(cmd.command_id)
    monkeypatch.setattr(cq, "get_supabase", lambda: _FakeSupabase({cmd.command_id}))  # remote has it
    mark_command_completed(cmd.command_id)
    assert _row(bus, cmd.command_id)[0] == "completed"  # mirrored to both buses, no error


def test_cq1_genuine_double_miss_logs_error(bus, monkeypatch):
    """0 rows in Supabase AND the command absent locally = a genuine completion failure -> ERROR."""
    from structlog.testing import capture_logs
    monkeypatch.setattr(cq, "get_supabase", lambda: _FakeSupabase(set()))
    with capture_logs() as logs:
        mark_command_completed("does-not-exist-anywhere")
    assert any(e["event"] == "control_command_mark_terminal_failed" for e in logs), logs


def test_cq1_already_terminal_local_is_idempotent_noop(bus, monkeypatch):
    """A 0-row local UPDATE on an ALREADY-terminal command is fine (no ERROR)."""
    from structlog.testing import capture_logs
    monkeypatch.setattr(cq, "get_supabase", lambda: None)
    cmd = enqueue_control_command_local(ControlCommandType.PING, {})
    try_claim_command(cmd.command_id)
    mark_command_completed(cmd.command_id)                      # -> completed
    with capture_logs() as logs:
        mark_command_completed(cmd.command_id)                  # re-complete: 0 rows, but present
    assert not any(e["event"] == "control_command_mark_terminal_failed" for e in logs)
    assert _row(bus, cmd.command_id)[0] == "completed"


def test_cq2_periodic_sweep_calls_the_sweep_functions(bus, monkeypatch):
    """The periodic sweep coroutine drains an expired orphan on its first iteration."""
    import asyncio

    from datetime import datetime, timedelta, timezone

    import src.engine.orchestrator as orch

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cmd = enqueue_control_command_local(ControlCommandType.PING, {}, expires_at=past)
    assert try_claim_command(cmd.command_id)                    # orphaned + expired

    sweep = orch.TradingOrchestrator._periodic_command_sweep

    async def _drive():
        task = asyncio.ensure_future(sweep(object(), None, interval_seconds=0.01))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())
    assert _row(bus, cmd.command_id)[0] == "failed", "periodic sweep should have retired the expired orphan"
