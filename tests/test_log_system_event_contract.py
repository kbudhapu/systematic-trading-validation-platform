"""GV-8 (Finding B, task_b2762026) — the log_system_event twin-signature contract.

Live incident 2026-07-16 (18:30 / 19:47 / 21:30 UTC): a Supabase ConnectionTerminated hit an error
path that itself threw `log_system_event() got an unexpected keyword argument 'metadata'` →
cycle_failed streak → BOTH legs SOFT-degraded. The local twin (persistence.db) was born without
`metadata`; the Supabase twin (SupabaseSync) was born with it, and five call sites were written
against the richer contract. These tests pin the two signatures together so the divergence class
cannot recur silently, and prove the formerly-crashing call shapes are absorbed.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

from src.control.supabase_sync import SupabaseSync
from src.persistence import db as pdb


REQUIRED_PARAMS = {"event_type", "message", "severity", "metadata"}


# ── the contract: both twins accept the same caller-facing signature ─────────
def test_both_twins_accept_the_same_caller_params():
    local = set(inspect.signature(pdb.log_system_event).parameters)
    remote = set(inspect.signature(SupabaseSync.log_system_event).parameters) - {"self"}
    assert REQUIRED_PARAMS <= local, f"local twin missing {REQUIRED_PARAMS - local}"
    assert REQUIRED_PARAMS <= remote, f"supabase twin missing {REQUIRED_PARAMS - remote}"
    # No caller-facing divergence beyond implementation-detail params (db_path is local-only).
    assert (local - {"db_path"}) == remote, (
        f"twin signatures diverged: local-only {local - {'db_path'} - remote}, "
        f"remote-only {remote - local}"
    )


def test_metadata_defaults_to_none_in_both():
    assert inspect.signature(pdb.log_system_event).parameters["metadata"].default is None
    assert (
        inspect.signature(SupabaseSync.log_system_event).parameters["metadata"].default is None
    )


# ── regression: the five formerly-crashing call shapes are absorbed ──────────
def _fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "events.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT
            )
            """
        )
    return db_path


def test_local_twin_absorbs_metadata_and_persists_it(tmp_path):
    db_path = _fresh_db(tmp_path)
    pdb.log_system_event(
        "FEED_SEQUENCE_MISALIGNMENT",
        "out-of-order vendor sequence",
        severity="warning",
        metadata={"symbol": "QQQ", "timeframe": "15Min"},  # market_data_stream.py:294 shape
        db_path=db_path,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT event_type, severity, metadata FROM system_events"
        ).fetchone()
    assert row[0] == "FEED_SEQUENCE_MISALIGNMENT"
    assert json.loads(row[2]) == {"symbol": "QQQ", "timeframe": "15Min"}


def test_local_twin_upgrades_pre_metadata_schema_in_place(tmp_path):
    """A pre-existing local DB (no metadata column) is upgraded additively, not crashed."""
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
    pdb.log_system_event(
        "supabase_fetch_failed",
        "<ConnectionTerminated error_code:1>",  # the live 18:30 shape (orchestrator handler)
        severity="warning",
        metadata={"error_code": 1},
        db_path=db_path,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT metadata FROM system_events").fetchone()
    assert json.loads(row[0]) == {"error_code": 1}


def test_metadata_none_still_works_everywhere(tmp_path):
    db_path = _fresh_db(tmp_path)
    pdb.log_system_event("plain_event", "no metadata", db_path=db_path)  # legacy call shape
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT metadata FROM system_events").fetchone()
    assert row[0] is None


def test_simulated_connection_terminated_handler_does_not_raise(tmp_path):
    """The regression the queue names: a Supabase ConnectionTerminated is absorbed by the
    error handler — the log_system_event call inside it must not throw (no cycle_failed streak,
    no leg degrade). Exercises every one of the five call-site shapes."""
    db_path = _fresh_db(tmp_path)
    shapes = [
        ("control_command_fetch_failed", {"error": "<ConnectionTerminated error_code:1>"}),
        ("cycle_error", {"streak": 1, "phase": "telemetry"}),        # orchestrator:1850/1869 class
        ("stream_status", {"symbol": "QQQ", "status": "halt"}),      # market_data_stream:121
        ("FEED_SEQUENCE_MISALIGNMENT", {"symbol": "QQQ", "timeframe": "15Min"}),  # :294
        ("FEED_SEQUENCE_MISALIGNMENT", {"symbol": "BTC/USD", "timeframe": "1Hour"}),  # :357
    ]
    for event_type, metadata in shapes:
        pdb.log_system_event(event_type, "handler shape", severity="warning",
                             metadata=metadata, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0]
    assert n == len(shapes)
