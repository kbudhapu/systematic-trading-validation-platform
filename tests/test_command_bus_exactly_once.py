"""E2 — Supabase bus-of-record: exactly-once claim across the dual bus + forward mirroring.

Closes CP2's double-execute window (a command dual-written to Supabase AND local SQLite must be
claimed exactly once) and verifies terminal status + local history mirror forward to Supabase.
"""
from __future__ import annotations

import sqlite3

import pytest

import src.control.command_queue as cq
from src.control.command_queue import (
    ControlCommandType,
    enqueue_control_command_local,
    mark_command_completed,
    mirror_local_commands_to_supabase,
    try_claim_command,
)


# --- a minimal in-memory fake of the Supabase PostgREST chain -----------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store):
        self.store = store
        self._op = None
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._op = "update"; self._payload = payload; return self

    def insert(self, row):
        self._op = "insert"; self._payload = row; return self

    def upsert(self, row, on_conflict=None):
        self._op = "upsert"; self._payload = row; return self

    def select(self, *_cols):
        self._op = "select"; return self

    def eq(self, k, v):
        self._filters[k] = v; return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._op == "update":
            changed = []
            for row in self.store.values():
                if all(row.get(k) == v for k, v in self._filters.items()):
                    row.update(self._payload); changed.append(dict(row))
            return _Resp(changed)
        if self._op == "select":
            return _Resp([dict(r) for r in self.store.values()
                          if all(r.get(k) == v for k, v in self._filters.items())])
        if self._op in ("insert", "upsert"):
            r = dict(self._payload); self.store[r["command_id"]] = r; return _Resp([r])
        return _Resp([])


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, dict] = {}

    def table(self, _name):
        return _Query(self.store)


@pytest.fixture
def tmp_bus(tmp_path, monkeypatch):
    db = tmp_path / "trading.db"
    monkeypatch.setattr(cq, "DB_PATH", db)
    monkeypatch.setattr(cq, "ensure_db_writable", lambda *a, **k: None)
    return db


def _local_status(db, command_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM control_commands WHERE command_id = ?",
                           (command_id,)).fetchone()
    return row["status"] if row else None


def test_local_only_claim_exactly_once(tmp_bus, monkeypatch):
    monkeypatch.setattr(cq, "get_supabase", lambda: None)
    enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, command_id="c1", db_path=tmp_bus)
    assert try_claim_command("c1") is True
    assert try_claim_command("c1") is False           # already processing → not re-claimed
    assert _local_status(tmp_bus, "c1") == "processing"


def test_dual_bus_claim_exactly_once(tmp_bus, monkeypatch):
    """The SAME command_id present in BOTH buses (CP2 window) is claimed exactly once and the
    local mirror is driven to processing so it can never be re-claimed on the next poll."""
    fake = FakeSupabase()
    fake.store["c1"] = {"command_id": "c1", "command_type": "RELOAD_CONFIG",
                        "status": "pending", "requested_by": "dashboard"}
    monkeypatch.setattr(cq, "get_supabase", lambda: fake)
    enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, command_id="c1", db_path=tmp_bus)

    assert try_claim_command("c1") is True            # claimed on the authoritative Supabase bus
    assert fake.store["c1"]["status"] == "processing"
    assert _local_status(tmp_bus, "c1") == "processing"   # local mirror driven forward
    # A second attempt must NOT re-claim (would be the double-execute): Supabase shows it present
    # and not pending, so the local copy is not independently claimed.
    assert try_claim_command("c1") is False


def test_mark_terminal_mirrors_both_buses(tmp_bus, monkeypatch):
    fake = FakeSupabase()
    fake.store["c1"] = {"command_id": "c1", "command_type": "RELOAD_CONFIG", "status": "processing"}
    monkeypatch.setattr(cq, "get_supabase", lambda: fake)
    enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, command_id="c1", db_path=tmp_bus)
    cq.try_claim_command("c1")  # drive local to processing too

    mark_command_completed("c1")
    assert fake.store["c1"]["status"] == "completed"
    assert _local_status(tmp_bus, "c1") == "completed"


def test_mirror_local_history_forward(tmp_bus, monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(cq, "get_supabase", lambda: fake)
    enqueue_control_command_local(ControlCommandType.RELOAD_CONFIG, command_id="h1", db_path=tmp_bus)
    enqueue_control_command_local(ControlCommandType.PING, command_id="h2", db_path=tmp_bus)

    assert fake.store == {}                     # Supabase empty (local-only history, CP2)
    n = mirror_local_commands_to_supabase()
    assert n == 2
    assert set(fake.store) == {"h1", "h2"}      # now visible to the dashboard
    assert fake.store["h1"]["status"] == "pending"
